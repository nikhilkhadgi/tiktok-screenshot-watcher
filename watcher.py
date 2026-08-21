import base64
import json
import os
import queue
import re
import signal
import sys
import threading
import time
from pathlib import Path

import requests
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from dotenv import load_dotenv
load_dotenv()  # Loads environment variables from .env file

# Configuration
WATCH_DIRECTORY = os.getenv("WATCH_DIRECTORY", "/home/ubuntu/Downloads/TikTok Live")
STATE_DIR = os.getenv("STATE_DIR", ".")
POCKETBASE_URL = os.getenv("POCKETBASE_URL", "http://127.0.0.1:8090")
COLLECTION_NAME = os.getenv("POCKETBASE_COLLECTION_NAME", "auction_items")

# Watch behaviour
OBSERVER_MODE = os.getenv("WATCHER_OBSERVER", "auto").strip().lower()
POLL_INTERVAL = float(os.getenv("WATCHER_POLL_INTERVAL") or 5)

# How often to drop ledger entries for screenshots the cleanup service has
# already removed. A show produces 1200-2000 files, so the ledger legitimately
# holds thousands of live entries; pruning is paced by time rather than size.
LEDGER_PRUNE_INTERVAL_SECONDS = float(os.getenv("LEDGER_PRUNE_INTERVAL_SECONDS") or 3600)

# A file untouched for this long is already fully written, so the stability
# sampling below can be skipped. Without this, a backlog of 2000 screenshots
# would spend ~50 minutes confirming that settled files had stopped changing.
SETTLED_AGE_SECONDS = 10.0

# EvoLink AI Configuration
EVOLINK_API_TOKEN = os.getenv("EVOLINK_API_TOKEN")
MODEL_NAME = os.getenv("EVOLINK_MODEL_NAME") or "gemini-3.5-flash-lite"
# `or` rather than a getenv default: compose passes an empty string when the
# variable is unset, and an empty string is not the same as absent.
EVOLINK_URL = os.getenv("EVOLINK_URL") or (
    f"https://direct.evolink.ai/v1beta/models/{MODEL_NAME}:generateContent"
)

if not EVOLINK_API_TOKEN:
    raise RuntimeError("EVOLINK_API_TOKEN is not set. Export it before running watcher.py.")

# The Chrome extension names each capture from the Live Manager page itself:
# Part1_SKU7_20260717_213256.png. Part and SKU are read out of the page, so the
# filename is authoritative for the item number in a way that reading it back
# off the pixels is not.
#
# Kept identical to the pattern in tiktok-order-tracker/screenshots.py, which
# resolves TikTok orders to these same files. The two must agree on what counts
# as a screenshot or an item can be seen by one and not the other.
SCREENSHOT_NAME_RE = re.compile(
    r"^Part(?P<part>\d+)_SKU(?P<sku>\d+)_(?P<date>\d{8})_(?P<time>\d{6})\.png$",
    re.IGNORECASE,
)

# Filesystem types that cannot deliver inotify events for writes made outside
# the container: Docker Desktop's older bind-mount transports, network shares,
# and FUSE passthroughs. Watching these requires polling instead.
#
# Deliberately absent: "fakeowner", the ownership-mapping layer Docker Desktop
# puts over VirtioFS. Host writes were measured to reach the container through
# it (Docker Desktop 28.0.4, Apple Silicon), so it is left to use inotify.
EVENTLESS_FILESYSTEMS = {
    "virtiofs", "fuse.grpcfuse", "grpcfuse", "osxfs", "9p",
    "nfs", "nfs4", "cifs", "smbfs", "smb3",
    "fuse.sshfs", "fuse.rclone", "fuse.s3fs",
}


def parse_item_number(name):
    """
    Derive 'Part X Item #Y' from a screenshot filename, or None if the name does
    not follow the convention. int() drops any zero padding so the result is
    stable regardless of how the extension formats the numbers.
    """
    match = SCREENSHOT_NAME_RE.match(os.path.basename(name))
    if not match:
        return None
    return f"Part {int(match.group('part'))} Item #{int(match.group('sku'))}"


def is_screenshot(name):
    return SCREENSHOT_NAME_RE.match(os.path.basename(name)) is not None


def _unescape_mount(path: str) -> str:
    """/proc/mounts octal-escapes spaces and a few other characters."""
    return (path.replace("\\040", " ")
                .replace("\\011", "\t")
                .replace("\\012", "\n")
                .replace("\\134", "\\"))


def _mount_fstype(path):
    """Filesystem type of the mount containing path, or None if undetermined."""
    try:
        with open("/proc/mounts", "r") as fh:
            lines = fh.readlines()
    except OSError:
        return None

    target = os.path.realpath(path)
    best_point, best_type = None, None
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        mount_point, fstype = _unescape_mount(fields[1]), fields[2]
        if target == mount_point or target.startswith(mount_point.rstrip("/") + "/"):
            if best_point is None or len(mount_point) > len(best_point):
                best_point, best_type = mount_point, fstype
    return best_type


def build_observer():
    """
    Pick a watch strategy. Native events (inotify on Linux, FSEvents on macOS)
    are cheap but only see writes made on the same kernel and a local
    filesystem. Docker Desktop bind mounts and network shares deliver nothing
    at all, so those have to be polled.
    """
    mode = OBSERVER_MODE
    if mode not in ("auto", "native", "polling"):
        print(f"Unknown WATCHER_OBSERVER={mode!r}; falling back to auto.")
        mode = "auto"

    if mode == "auto":
        fstype = _mount_fstype(WATCH_DIRECTORY)
        if fstype in EVENTLESS_FILESYSTEMS:
            mode, reason = "polling", f"{fstype} delivers no events for outside writes"
        elif fstype is not None:
            mode, reason = "native", f"{fstype} supports inotify"
        elif sys.platform == "darwin":
            mode, reason = "native", "macOS FSEvents"
        else:
            mode, reason = "polling", "filesystem type could not be determined"
        print(f"Watch strategy: {mode} ({reason})")
    else:
        print(f"Watch strategy: {mode} (forced by WATCHER_OBSERVER)")

    if mode == "polling":
        print(f"Poll interval: {POLL_INTERVAL}s")
        return PollingObserver(timeout=POLL_INTERVAL)
    return Observer()


def wait_for_stable_size(file_path, checks=3, interval=0.5, timeout=60.0):
    """
    Wait until a file stops growing before reading it. Downloads arrive
    incrementally and polling can notice them mid-write. Returns the settled
    size, or None if the file vanished or never stopped changing.
    """
    try:
        stat = os.stat(file_path)
    except OSError:
        return None

    # Already settled: skip the sampling loop entirely. This is the common case
    # for the startup backlog, where every file has been on disk for hours.
    if stat.st_size > 0 and (time.time() - stat.st_mtime) > SETTLED_AGE_SECONDS:
        return stat.st_size

    deadline = time.monotonic() + timeout
    last_size, stable_reads = -1, 0

    while time.monotonic() < deadline:
        try:
            size = os.path.getsize(file_path)
        except OSError:
            return None

        if size > 0 and size == last_size:
            stable_reads += 1
            if stable_reads >= checks:
                return size
        else:
            stable_reads = 0
            last_size = size

        time.sleep(interval)

    return None


class ProcessedLedger:
    """
    Records which screenshots have been ingested so a restart does not re-send
    them to the API. Keyed by filename and size, so a file replaced under the
    same name is picked up again.
    """

    def __init__(self, path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._entries = self._load()
        self._last_prune = time.monotonic()
        print(f"Ledger: {self._path} ({len(self._entries)} entries)")

    def _load(self):
        try:
            with open(self._path, "r") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            print(f"Could not read ledger at {self._path} ({e}); starting empty.")
            return {}
        return data if isinstance(data, dict) else {}

    def contains(self, file_path, size):
        with self._lock:
            entry = self._entries.get(os.path.basename(file_path))
        return isinstance(entry, dict) and entry.get("size") == size

    def add(self, file_path, size):
        with self._lock:
            self._entries[os.path.basename(file_path)] = {
                "size": size,
                "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self._flush()

    def prune_missing(self, directory):
        """
        Drop entries whose screenshot is no longer in the watch directory.
        Those files are deleted by a separate service once it has finished with
        them, and a filename that is gone can never be scanned again, so the
        entry is dead weight. Returns the number removed.
        """
        with self._lock:
            stale = [name for name in self._entries
                     if not os.path.exists(os.path.join(directory, name))]
            for name in stale:
                del self._entries[name]
            if stale:
                self._flush()
            self._last_prune = time.monotonic()
        return len(stale)

    def maybe_prune(self, directory):
        """Prune mid-run at most once per interval, for long-lived processes."""
        with self._lock:
            due = (time.monotonic() - self._last_prune) >= LEDGER_PRUNE_INTERVAL_SECONDS
        if due:
            removed = self.prune_missing(directory)
            if removed:
                print(f"Ledger: pruned {removed} entries for deleted screenshots.")

    def _flush(self):
        # Write to a sibling temp file and rename, so a crash mid-write cannot
        # leave a truncated ledger behind.
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w") as fh:
                json.dump(self._entries, fh, indent=2, sort_keys=True)
            os.replace(tmp_path, self._path)
        except OSError as e:
            print(f"Warning: could not persist ledger: {e}")


class ScreenshotHandler(FileSystemEventHandler):
    """
    Queues screenshots and ingests them one at a time on a worker thread, so a
    slow API call never blocks the watchdog emitter or overlaps with itself.
    """

    def __init__(self, ledger):
        super().__init__()
        self.ledger = ledger
        self._queue = queue.Queue()
        self._pending = set()
        self._lock = threading.Lock()

    def on_created(self, event):
        if not event.is_directory:
            self._maybe_submit(event.src_path)

    def on_moved(self, event):
        # Chrome downloads land as *.crdownload and are renamed on completion,
        # which arrives as a move rather than a create.
        if not event.is_directory:
            self._maybe_submit(event.dest_path)

    def _maybe_submit(self, file_path):
        if not file_path or not file_path.lower().endswith(".png"):
            return
        # A PNG that lands here with the wrong name carries no item number, so
        # it is reported rather than dropped silently -- unlike the download
        # temp files and editor droppings filtered out above.
        if not is_screenshot(file_path):
            print(f"Ignoring {os.path.basename(file_path)}: "
                  "not named Part<X>_SKU<Y>_<date>_<time>.png")
            return
        print(f"New screenshot detected: {file_path}")
        self.submit(file_path)

    def submit(self, file_path):
        with self._lock:
            if file_path in self._pending:
                return
            self._pending.add(file_path)
        self._queue.put(file_path)

    def run_worker(self):
        while True:
            file_path = self._queue.get()
            try:
                if file_path is None:
                    return
                self.ingest(file_path)
            except Exception as e:
                print(f"Error processing {file_path}: {e}")
            finally:
                if file_path is not None:
                    with self._lock:
                        self._pending.discard(file_path)
                self._queue.task_done()

    def stop_worker(self):
        self._queue.put(None)

    def ingest(self, file_path):
        # Resolved first: a name that cannot be parsed has no item number, and
        # there is no point paying for extraction on a record we cannot key.
        item_number = parse_item_number(file_path)
        if item_number is None:
            print(f"Skipping {os.path.basename(file_path)}: "
                  "filename does not match Part<X>_SKU<Y>_<date>_<time>.png")
            return

        size = wait_for_stable_size(file_path)
        if size is None:
            print(f"Skipping {file_path}: file disappeared or never settled.")
            return

        if self.ledger.contains(file_path, size):
            print(f"Skipping {os.path.basename(file_path)}: already processed.")
            return

        extracted_data = self.process_image_with_gemini(file_path)
        print(f"Extracted Data: {item_number} -> {extracted_data}")

        # Only recorded on success, so a transient failure is retried on the
        # next restart rather than being silently dropped.
        if self.save_to_pocketbase(file_path, item_number, extracted_data):
            self.ledger.add(file_path, size)
            self.ledger.maybe_prune(WATCH_DIRECTORY)

    def process_image_with_gemini(self, image_path):
        # Read and base64 encode the image
        with open(image_path, "rb") as image_file:
            encoded_image = base64.b64encode(image_file.read()).decode('utf-8')

        prompt_text = (
            "Analyze this screenshot from a live shopping auction.\n"
            "Extract the following fields and return ONLY a valid JSON object with these exact keys:\n"
            "- 'name': The clear product name. Validate if the product is real and correct it if necessary.\n"
            "- 'retail_price': Search online to find out the numeric retail price in USD as a float or string (e.g., 29.99)."
        )

        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt_text},
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": encoded_image
                            }
                        }
                    ]
                }
            ]
        }

        headers = {
            "Authorization": f"Bearer {EVOLINK_API_TOKEN}",
            "Content-Type": "application/json"
        }

        response = requests.post(EVOLINK_URL, json=payload, headers=headers)

        if response.status_code != 200:
            raise Exception(f"EvoLink API error: {response.text}")

        res_json = response.json()

        try:
            text_content = res_json['candidates'][0]['content']['parts'][0]['text']
        except (KeyError, IndexError):
            raise Exception(f"Unexpected response structure from API: {res_json}")

        # Strip markdown code block formatting if returned by the model
        clean_json = text_content.replace('```json', '').replace('```', '').strip()
        return json.loads(clean_json)

    def save_to_pocketbase(self, file_path, item_number, data):
        url = f"{POCKETBASE_URL}/api/collections/{COLLECTION_NAME}/records"

        # PocketBase rewrites an uploaded file's name -- lowercased, with a
        # random suffix before the extension -- so the `screenshot` field cannot
        # be matched against a name on disk. source_file keeps the extension's
        # name verbatim, which is unique to the second and is the key
        # tiktok-order-tracker uses to attach an order_id to this record.
        source_file = os.path.basename(file_path)

        payload = {
            "item_number": item_number,
            "name": data.get("name"),
            "retail_price": data.get("retail_price"),
            "source_file": source_file,
        }

        with open(file_path, "rb") as f:
            files = {"screenshot": (source_file, f)}
            response = requests.post(url, data=payload, files=files)

        if response.status_code in [200, 201]:
            print(f"Successfully saved item {item_number} to PocketBase!")
            return True

        print(f"Failed to save to PocketBase: {response.text}")
        return False


def scan_existing(handler, ledger, directory):
    """
    Queue anything that arrived while the watcher was down. Files already in
    the ledger are skipped quietly so a large watch directory stays readable
    in the logs.
    """
    try:
        names = sorted(os.listdir(directory))
    except OSError as e:
        print(f"Startup scan: could not read {directory}: {e}")
        return

    pending = []
    ignored = 0
    for name in names:
        if not name.lower().endswith(".png"):
            continue
        if not is_screenshot(name):
            ignored += 1
            continue
        path = os.path.join(directory, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if not ledger.contains(path, size):
            pending.append(path)

    if ignored:
        print(f"Startup scan: ignored {ignored} PNG(s) not matching the naming convention.")

    if not pending:
        print("Startup scan: nothing new.")
        return

    print(f"Startup scan: queueing {len(pending)} unprocessed screenshot(s).")
    for path in pending:
        handler.submit(path)


def main():
    os.makedirs(WATCH_DIRECTORY, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)

    ledger = ProcessedLedger(os.path.join(STATE_DIR, "processed.json"))
    removed = ledger.prune_missing(WATCH_DIRECTORY)
    if removed:
        print(f"Ledger: pruned {removed} entries for screenshots already cleaned up.")

    handler = ScreenshotHandler(ledger)

    worker = threading.Thread(target=handler.run_worker, name="ingest", daemon=True)
    worker.start()

    observer = build_observer()
    observer.schedule(handler, path=WATCH_DIRECTORY, recursive=False)

    print(f"Starting watcher on directory: {WATCH_DIRECTORY}...")
    observer.start()

    # Started after the observer so files landing during the scan are caught by
    # one path or the other; the pending set keeps them from being done twice.
    scan_existing(handler, ledger, WATCH_DIRECTORY)

    stop_event = threading.Event()

    def request_stop(signum, _frame):
        print(f"Received signal {signum}; shutting down.")
        stop_event.set()

    # SIGTERM is what `docker compose stop` sends.
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    stop_event.wait()

    observer.stop()
    observer.join()
    handler.stop_worker()
    worker.join(timeout=30)
    print("Watcher stopped.")


if __name__ == "__main__":
    main()
