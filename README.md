# TikTok PocketBase Watcher

Automates the flow from screenshot to structured record:

1. Watches a folder for new image files.
2. Sends each image to EvoLink's Gemini endpoint for extraction.
3. Saves the extracted data and screenshot to PocketBase.

A Chrome extension captures screenshots from the TikTok Live Manager page into a
download folder; this service picks them up from there.

## Quick start (Docker)

Requires Docker with Compose v2. Both the watcher and PocketBase run as containers,
so no Python, virtualenv, or PocketBase binary is needed on the host.

```bash
cp .env.example .env
```

Edit `.env` and set two things:

- `EVOLINK_API_TOKEN` — your EvoLink bearer token.
- `HOST_WATCH_DIR` — the folder your Chrome extension downloads into. This is
  the only value that differs between machines.

On Linux, also set `PUID`/`PGID` to the output of `id -u` and `id -g` so the
container can read the bind-mounted folders.

Create the bind-mount directories before the first start. Docker will invent any
that are missing, but it creates them as **root**, which the container user then
cannot write to:

```bash
mkdir -p state pb_data
docker compose up -d --build
docker compose logs -f watcher
```

That's it. The stack restarts on boot and after crashes.

### Managing the stack

```bash
docker compose ps                  # status of both services
docker compose logs -f watcher     # follow watcher output
docker compose restart watcher     # restart just the watcher
docker compose down                # stop both (data and state are preserved)
docker compose up -d --build       # rebuild after changing watcher.py
```

## How it runs on both macOS and Linux

The same compose file serves both machines. Container paths are fixed; only the
host paths vary.

| Inside the container | Host side | Set by |
| --- | --- | --- |
| `/watch` | your Chrome download folder | `HOST_WATCH_DIR` |
| `/pb_data` | `./pb_data` | fixed |
| `/state` | `./state` | fixed |

**Watch strategy.** File-change notifications (inotify on Linux, FSEvents on
macOS) are cheap, but only work when the watcher shares a kernel and a local
filesystem with whatever writes the files. Docker Desktop bind mounts and
network shares deliver no events at all — the watcher would sit there silently
and never process anything.

`WATCHER_OBSERVER=auto` (the default) handles this by reading `/proc/mounts` and
checking the filesystem type behind `/watch`:

- Linux VPS → `ext4` → native inotify.
- Docker Desktop on macOS with VirtioFS → `fakeowner` → native inotify. This was
  measured, not assumed: a host-written file was picked up by the container on
  Docker Desktop 28.0.4 / Apple Silicon.
- Docker Desktop set to gRPC-FUSE or osxfs → `fuse.grpcfuse` / `osxfs` → polling
  every `WATCHER_POLL_INTERVAL` seconds.
- NFS, SMB, sshfs → polling.

The chosen strategy and the reason are printed at startup, so check the first
lines of `docker compose logs watcher` if files are not being picked up. If
detection ever gets it wrong, `WATCHER_OBSERVER=native` and
`WATCHER_OBSERVER=polling` force the decision either way.

**Architecture.** The PocketBase image downloads the Linux release matching the
build platform, so the same Dockerfile produces an arm64 image on Apple Silicon
and an amd64 image on the VPS. The `pocketbase` binary in this repo is a macOS
build used for local non-Docker work; it is never copied into an image.

## Restart behaviour

Files that arrive while the watcher is down are not lost. On startup it scans
the watch directory and queues anything absent from its ledger at
`state/processed.json`, which is keyed by filename and size.

A screenshot is recorded in the ledger only after PocketBase accepts the record.
A file that fails — bad API response, malformed JSON, PocketBase rejection — is
therefore retried on the next restart rather than silently dropped. If a file
fails permanently it will be retried at every restart, so watch the logs for a
filename that keeps reappearing.

This watcher never moves or deletes screenshots. A separate service consumes the
same folder and removes them the following day, which is what keeps the
directory bounded.

The ledger follows the folder. On startup, and hourly thereafter, entries whose
screenshot is no longer on disk are dropped — a filename that has been cleaned
up can never be scanned again, so the entry cannot match anything. The ledger
therefore stays roughly the size of the watch directory instead of growing
forever.

At 1200-2000 screenshots per show the ledger legitimately holds thousands of
live entries, so pruning is paced by time (`LEDGER_PRUNE_INTERVAL_SECONDS`)
rather than by a size threshold.

## Throughput

The extension captures one screenshot every 10 seconds, so a show runs 1200-2000
files over 3.5-5.5 hours. Ingestion is deliberately serialized on a single
worker thread, which gives each screenshot a 10-second budget: roughly 1.5s of
stability sampling, the Gemini round trip, and the PocketBase insert. That
leaves about 2x headroom, so no queue builds during a show and no worker pool is
needed.

If the API ever slows past ~8s per call the queue will grow during the show and
drain afterwards. Nothing is lost when that happens — the ledger means a restart
mid-backlog re-extracts nothing.

The ledger still earns its place despite the cleanup: it covers the window
between a screenshot being ingested and the other service removing it, which is
about a day. Without it, any restart in that window would re-extract and
duplicate everything still sitting in the folder.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `EVOLINK_API_TOKEN` | none | Required EvoLink bearer token |
| `HOST_WATCH_DIR` | none | Required. Host folder mounted at `/watch` |
| `EVOLINK_MODEL_NAME` | `gemini-3.5-flash-lite` | Gemini model name |
| `EVOLINK_URL` | derived from model name | Pins the full endpoint. Setting it makes `EVOLINK_MODEL_NAME` have no effect |
| `WATCHER_OBSERVER` | `auto` | `auto`, `native`, or `polling` |
| `WATCHER_POLL_INTERVAL` | `5` | Seconds between scans in polling mode |
| `LEDGER_PRUNE_INTERVAL_SECONDS` | `3600` | How often to drop ledger entries for deleted screenshots |
| `POCKETBASE_COLLECTION_NAME` | `auction_items` | PocketBase collection name |
| `PB_VERSION` | `0.39.10` | PocketBase version built into the image |
| `PUID` / `PGID` | `1000` | Bind-mount owner. Required on Linux, ignored by Docker Desktop |
| `WATCH_DIRECTORY` | `/home/ubuntu/Downloads/TikTok Live` | Non-Docker only. Compose sets this to `/watch` |
| `POCKETBASE_URL` | `http://127.0.0.1:8090` | Non-Docker only. Compose sets this to `http://pocketbase:8090` |
| `STATE_DIR` | `.` | Non-Docker only. Compose sets this to `/state` |

## PocketBase

The collection is **not** created automatically. On a fresh `pb_data`, bring the
stack up, open the admin UI, and create a collection named `auction_items` with:

- `item_number` — Text
- `name` — Text
- `retail_price` — Number
- `screenshot` — File, single upload

The script creates records without authenticating, so leave the create rule empty.

Until the collection exists, every ingest fails with a 404 from PocketBase. The
watcher keeps running and does not record those files in its ledger, so they are
retried automatically once the collection is in place. To check what a data
directory contains:

```bash
sqlite3 pb_data/data.db "select name from _collections;"
```

**This is why the port binding matters.** PocketBase listens on `0.0.0.0:8090`
inside its container so the watcher can reach it over the compose network, but
compose publishes it as `127.0.0.1:8090:8090` — reachable from the host only.
Changing that to `8090:8090` would expose an unauthenticated write endpoint and
the admin UI to the internet.

Reach the admin UI at `http://127.0.0.1:8090/_/`. On the VPS, tunnel to it
rather than opening the port:

```bash
ssh -L 8090:127.0.0.1:8090 ubuntu@your-vps
```

`PB_VERSION` is pinned to the version that wrote the existing `pb_data`. A newer
PocketBase can migrate the data directory in place, so back up `pb_data` before
changing it.

## Running without Docker

Still supported. Requires Python 3.10+ and a PocketBase instance on the host.

```bash
cd ~/Projects/tiktok-screenshot-watcher
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Uncomment `WATCH_DIRECTORY`, `POCKETBASE_URL`, and `STATE_DIR` in `.env`, then:

```bash
python watcher.py
```

`watcher.py` calls `load_dotenv()` on startup, so `.env` is read from the working
directory automatically — no manual `export` is needed. Real environment
variables take precedence, so you can override any setting at launch:

```bash
WATCHER_OBSERVER=polling python watcher.py
```

### PocketBase on a VPS without Docker

```bash
sudo apt update
sudo apt install -y wget unzip curl
mkdir -p ~/Projects/tiktok-pocketbase
cd ~/Projects/tiktok-pocketbase
wget https://github.com/pocketbase/pocketbase/releases/download/v0.39.10/pocketbase_0.39.10_linux_amd64.zip
unzip pocketbase_0.39.10_linux_amd64.zip
chmod +x pocketbase
```

### systemd units

Two services, `pocketbase.service` and `tiktok-watcher.service`:

```ini
[Unit]
Description=PocketBase Server
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Projects/tiktok-pocketbase
ExecStart=/home/ubuntu/Projects/tiktok-pocketbase/pocketbase serve --http=127.0.0.1:8090
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```ini
[Unit]
Description=TikTok Screenshot Watcher Service
After=network.target pocketbase.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Projects/tiktok-screenshot-watcher
ExecStart=/home/ubuntu/Projects/tiktok-screenshot-watcher/venv/bin/python /home/ubuntu/Projects/tiktok-screenshot-watcher/watcher.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

`PYTHONUNBUFFERED=1` makes `print()` output appear immediately in the journal.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pocketbase tiktok-watcher
sudo systemctl status tiktok-watcher
sudo journalctl -u tiktok-watcher -f
```

## Project Files

- `watcher.py` — file watcher and ingestion script
- `compose.yaml` — two-service stack
- `Dockerfile` — watcher image
- `Dockerfile.pocketbase` — PocketBase image, pinned and multi-arch
- `requirements.txt` — Python dependencies
- `.env.example` — sample environment configuration

## Notes

- The API token comes from the environment or `.env`; it is not in the source
  and not baked into any image.
- Item numbers are normalized to `Part <X> Item #<Y>` in Python after the model
  responds, so out-of-order results like `#10 Part 1` still land consistently.
- Chrome writes downloads as `*.crdownload` and renames them on completion, so
  the watcher handles move events as well as create events, and waits for a
  file's size to stop changing before reading it.
- Ingestion is serialized on a single worker thread, so a slow API call cannot
  overlap with the next screenshot or block the watcher.
- Failures are logged per file and never stop the watcher.
