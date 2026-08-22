# The TikTok Live Auction Pipeline

Three separate repositories cooperate to answer one question: **given a TikTok
order ID, what physical product was in that order?**

This document is the shared context none of the three repos can hold on its
own. Read it before changing anything that crosses a boundary — the filename
convention in particular is a contract between all three, and each repo looks
self-contained enough that you can break the others without noticing.

Companion documents in this repo:

- **`DEPLOY_STEPS.txt`** — deployment order and the one-off migrations
- **`CRON_SETUP.txt`** — the nightly job, why each line of it exists, and how
  to read its output

Last updated 2026-08-22.

---

## 1. Why this exists

Beauty Bedazzled runs live "random pull" auctions on TikTok Shop. The host
pulls an item on camera, calls it by a number, and the winning bidder buys a
generic listing. The order that results looks like this:

```
product_name : "Part 1 $1 Beauty Auction! *NO CANCELS* 08/21"
sku_name     : "197"
order_id     : "577535499565437047"
```

Nothing there says the item was a *Rare Beauty Find Comfort Body Mist*. That
information exists only in the live video, and **TikTok keeps video receipts
for 30 days**. After that, a customer asking "what did I get in order X?" is
unanswerable.

The pipeline replaces the video receipt with a durable record: a screenshot of
each item as it was pulled, an AI-extracted product name and price, and the
order ID attached to it.

---

## 2. The three repositories

| Repo | Path | Remote | Role |
|---|---|---|---|
| **tiktok-live-screenshot** | `~/Claude/Projects/tiktok-live-screenshot` | `nikhilkhadgi/tiktok-live-screenshot` | Chrome extension. Captures a video frame per auction item |
| **tiktok-screenshot-watcher** | `~/Projects/tiktok-screenshot-watcher` | `nikhilkhadgi/tiktok-screenshot-watcher` | Watches for screenshots, extracts product data via AI, writes to PocketBase. Also **hosts PocketBase** |
| **tiktok-order-tracker** | `~/Projects/tiktok-order-tracker` | `nikhilkhadgi/beautybedazzled-order-tracker` | Generates packing manifests from TikTok orders; backfills `order_id` into PocketBase |

Branches, as of this writing:

- Extension: **`main`**, version 2.3.0 — v2 has been merged in. The VPS still
  runs an older build (v1) until capture moves to Texas; that is fine, because
  the UTC change is inert on a UTC host (§5).
- Watcher: `main`.
- Tracker: `main`.

---

## 3. End-to-end data flow

```
  TikTok Live Manager page (shop.tiktok.com/streamer*)
        │  pin card reads "#197 Part 1"
        ▼
  ┌─────────────────────────────────────────────┐
  │ EXTENSION  content.js                       │
  │   poll 500ms → detect new Part+SKU          │
  │   wait 3000ms → capture video frame         │
  │   draw "Part 1   Item 197" label on frame   │
  │   name it Part1_SKU197_20260822_005141.webp │  ← UTC
  └─────────────────────────────────────────────┘
        │ saves to local Downloads folder, and/or S3
        ▼
  ┌─────────────────────────────────────────────┐
  │ WATCHER  watcher.py                         │
  │   parse Part+SKU from the FILENAME          │
  │   send image to EvoLink/Gemini              │
  │     → product name, retail price            │
  │   POST to PocketBase (unauthenticated)      │
  └─────────────────────────────────────────────┘
        │
        ▼
     PocketBase  auction_items
        item_number  "Part 1 Item #197"
        name         "Rare Beauty Find Comfort Body Mist"
        retail_price 28
        screenshot   <file>
        source_file  "Part1_SKU197_20260822_005141.webp"  ← the join key
        order_id     "577535499565437047"                  ← filled nightly
        ▲
        │ PATCH order_id
  ┌─────────────────────────────────────────────┐
  │ TRACKER  fill_order_ids.py                  │
  │   TikTok Order API → orders + line items    │
  │   parse_sku_part()  → (part, sku)           │
  │   lookup_screenshot() → filename            │
  │   match on source_file → PATCH order_id     │
  └─────────────────────────────────────────────┘
        │
        │ (same repo, separate flow)
        ▼
     Packing manifest PDF/HTML, with product photos
```

---

## 4. The filename is the contract

```
Part{X}_SKU{N}_{YYYYMMDD}_{HHMMSS}.{png|webp}
Part1_SKU197_20260822_005141.webp
```

**Both encodings are accepted everywhere.** The extension switched from PNG to
WebP q0.90 in v2.3 — a PNG frame ran 2.4–3.7 MB against roughly 400 KB for
WebP. Every screenshot captured before that is a PNG, and the archive has to
stay readable: a webp-only pattern would empty the index and silently strip the
photos from every historical manifest. Do not "tidy" this to one extension.

This one string is the join key for the entire system. It is:

- the **S3 object key** (grouped by date: `tiktok-live/20260822/…`)
- **`auction_items.source_file`** in PocketBase, stored verbatim
- what **`lookup_screenshot`** resolves a TikTok order to

The same regex is duplicated in two repos and **must stay identical**:

| Repo | Location |
|---|---|
| Watcher | `watcher.py` → `SCREENSHOT_NAME_RE` (plus `IMAGE_EXTENSIONS`) |
| Tracker | `screenshots.py` → `_FILENAME_RE` |
| Tracker | `fill_order_ids.py` → `_FILE_DATE_RE` (date extraction only) |

The extension builds it in `content.js` → `buildFilename()`, and derives the S3
prefix from it in `s3.js` → `buildS3Key()` via `/_(\d{8})_/`.

**Why `source_file` and not `item_number`:** item numbers restart every show,
so "Part 1 Item #7" is ambiguous across dates. The filename carries the date
and time and is unique to the second.

**Why `source_file` and not the `screenshot` field:** PocketBase rewrites
uploaded filenames — lowercased with a random suffix. `Part1_SKU197_…png`
becomes `part1_sku197_…_72thx4su8h.png`. It cannot be matched against a name
on disk.

It is, however, **reversible**. The rewrite only lowercases and inserts a
random 10-character token before the extension, so part, SKU, date and time all
survive and the record can be tied back to the exact file it came from:

```
part1_sku1000_20260822_065312_k1a4d1iol8.png
     ^1   ^1000  ^20260822 ^065312        ^token
```

That is how `source_file` was backfilled onto records predating the field.
Resolve `(part, sku, timestamp)` against the screenshot index and copy the real
name off disk rather than rebuilding it from the pieces — a guess with the
wrong casing or padding writes a value that silently never joins, and later
looks identical to a screenshot that was never captured.

---

## 5. The two-clock rule

This caused more bugs than anything else in the system. There are exactly two
clocks and they must not be confused.

| | Zone | Applies to |
|---|---|---|
| **Matching** | **UTC** | Screenshot filenames, S3 prefixes, `source_file`, TikTok `create_time`, every comparison in `screenshots.py` and `fill_order_ids.py` |
| **People** | **`SHOW_TZ`** (`America/Chicago`) | The date you type on the CLI, session times, order times, manifest headings |

`SHOW_TZ` is defined **once**, in the tracker's `config.py`. It previously
existed as `_CT` copied into four modules, and one of those copies reaching the
matcher is what made results depend on which machine ran them.

**Nothing reads the ambient system timezone.** No `TZ` needs to be set on any
host or container; setting one changes log timestamps and cron firing only.

### The offset that trips everyone up

A Texas evening falls on the **next UTC day** — 19:00 CDT is 00:00 UTC. So the
21 August show has:

- screenshots named `20260822_*`
- S3 prefix `tiktok-live/20260822/`
- but you still ask for it as `--date 2026-08-21`, and the manifest prints
  `2026-08-21`

That is intentional, not a bug. A show also lands almost wholly inside one UTC
day, which is why UTC filenames keep a show in a single S3 prefix.

### Why UTC in the filename rather than local

`buildFilename` originally used the local `Date` getters, so the timestamp
meant whatever timezone the capturing machine was set to — without recording
which. It only worked because the VPS runs `TZ=UTC`. Switching to `getUTC*`:

- is **inert on the VPS** (local already is UTC), so v1 needs no change
- fixes the Texas machine, which is CDT
- removes daylight-saving hazards. On the November fall-back night local times
  01:00–01:59 occur twice, and shows run into that window — two captures could
  otherwise collide on one S3 key and silently overwrite

---

## 6. Extension — `tiktok-live-screenshot`

Manifest V3, version 2.2.0. Content script runs on
`*://shop.tiktok.com/streamer*`.

| File | Purpose |
|---|---|
| `content.js` | Poll loop, product detection, capture trigger, filename |
| `background.js` | Service worker; routes the PNG to local / S3 / both |
| `s3.js` | SigV4 signing, `buildS3Key` |
| `overlay.js` | Draws the `Part X   Item N` label onto the frame |
| `options.js/html` | Settings page |
| `popup.js/html` | Capture on/off toggle |

Key constants in `content.js`:

```js
const SCREENSHOT_DELAY_MS = 3000;      // wait after pin-card change before capturing
const POLL_INTERVAL_MS    = 500;       // how often to check for changes
const IMAGE_TYPE          = 'image/webp';
const IMAGE_QUALITY       = 0.90;
const IMAGE_EXT           = 'webp';
```

The image constants are declared in **both** `content.js` and `background.js`
(the two capture paths, direct-video and legacy-tab). Change one and change the
other, or the two paths emit different formats.

Product detection reads the pin card and parses `#197 Part 1`:

```js
function parseProductName(text) {
  const match = text.match(/#(\d+)\s+Part\s*(\d+)/i);
  ...
}
```

### Per-install settings (`chrome.storage.local`, not synced)

- `destination` — `local` | `s3` | `both`
- `s3Config` — bucket, region, **prefix**, access key, secret
- `captureMethod` — `video` (default) or legacy `tab`
- `capturingEnabled` — the popup toggle

`destination` and `prefix` are the levers for managing two capture machines
without any code change. See §11.

### A known, accepted behaviour

`checkPinCard` cancels a pending capture when a new item appears:

```js
if (screenshotTimeout) clearTimeout(screenshotTimeout);
```

So an item replaced within ~3.5s (3000ms timer + 500ms poll) is never
captured — silently, no log. **This was reviewed and accepted**: the auction
runs ~10s per item (5s at the fast end), and the archive shows zero missing
items across 849 consecutive captures. Do not re-raise it as a bug unless the
cadence drops below ~5s.

---

## 7. Watcher — `tiktok-screenshot-watcher`

A single file, `watcher.py`, plus a PocketBase container. This repo **hosts the
database** the other two depend on.

### Flow

1. `watchdog` observes the watch directory (auto-selects inotify vs polling
   based on filesystem type — Docker bind mounts and network shares get
   polling).
2. A new PNG is validated against `SCREENSHOT_NAME_RE`. Non-conforming PNGs are
   logged and skipped; there is no AI fallback, because mixed provenance is
   worse than a visible gap.
3. `parse_item_number()` derives `Part X Item #Y` from the **filename**. The
   model is never asked for it.
4. `wait_for_stable_size()` avoids reading a partially-written file. Files
   older than `SETTLED_AGE_SECONDS` (10s) skip the sampling loop, so a 2000-file
   backlog does not take 50 minutes.
5. The image goes to EvoLink's Gemini endpoint for `name` and `retail_price`.
   `image_mime_type()` derives the content type from the suffix. The endpoint
   was measured to sniff the real format regardless of the label, but a
   correct one costs nothing.
6. `save_to_pocketbase()` POSTs the record plus the file.
7. `ProcessedLedger` records filename+size in `state/processed.json` so a
   restart does not re-send. Only written on success, so transient failures
   retry on the next run.

### It has no timezone dependency

Audited and verified empirically under four timezones. Every time call is
either a monotonic clock, a difference between epoch values, or explicitly
`time.gmtime()`. The filename's date and time groups are captured by the regex
but **never read** — only `part` and `sku` are used.

### Configuration (`.env`)

```
EVOLINK_API_TOKEN, HOST_WATCH_DIR, EVOLINK_MODEL_NAME,
WATCHER_OBSERVER, WATCHER_POLL_INTERVAL, LEDGER_PRUNE_INTERVAL_SECONDS,
POCKETBASE_COLLECTION_NAME, PB_VERSION, PUID, PGID
```

Compose services: `pocketbase`, `watcher`.

---

## 8. PocketBase

Collection **`auction_items`**:

| Field | Type | Notes |
|---|---|---|
| `item_number` | Text | `Part 1 Item #197`, derived from the filename |
| `name` | Text | From Gemini |
| `retail_price` | Number | From Gemini |
| `screenshot` | File | **Renamed on upload** — do not use as a key |
| `source_file` | Text | The filename verbatim. **The join key** |
| `order_id` | Text | 19-digit TikTok ID. **Must be Text** |
| `created` / `updated` | auto | UTC. `created` is *ingest* time, not capture time |

### `order_id` must never be a Number

TikTok order IDs are 18–19 digits. PocketBase `number` is a float64, and
anything past 2^53 (~16 digits) silently rounds — `577535499565437047` would
become `…785280`. The API returns them as JSON strings for this reason.

### API rules

```
create = ''    → public   (this is how the watcher inserts without credentials)
list   = NULL  → superuser only
view   = NULL  → superuser only
update = NULL  → superuser only
delete = NULL  → superuser only
```

Consequence: anything that **reads back or patches** a record needs superuser
credentials. That is why `fill_order_ids.py` authenticates via
`/api/collections/_superusers/auth-with-password`, and why the port is bound to
`127.0.0.1` — a public create rule must never be reachable off-host.

Missing and worth adding: indexes on `source_file` and `order_id`. The first is
queried on every backfill (1200–2000 per show), the second on every customer
lookup.

---

## 9. Tracker — `tiktok-order-tracker`

| File | Lines | Purpose |
|---|---|---|
| `html_manifest.py` | 520 | HTML packing manifest with photos |
| `fill_order_ids.py` | 454 | **Backfills `order_id` into PocketBase** |
| `manifest.py` | 404 | PDF packing manifest |
| `auth.py` | 362 | TikTok OAuth, token cache, refresh |
| `orders.py` | 320 | Order fetch, models, session clustering, `parse_sku_part` |
| `main.py` | 249 | Interactive manifest CLI |
| `screenshots.py` | 201 | Screenshot index and `lookup_screenshot` |
| `client.py` | 125 | Signed TikTok API client |
| `config.py` | 66 | Config + `SHOW_TZ` |

### TikTok API

- `POST /order/202309/orders/search` — paginated, walked backwards in 6-hour
  windows to stay under the cursor cap
- `GET /order/202309/orders?ids=…` — detail, batched ≤50, with retry+backoff
- Auth: `app_key` + `app_secret` + per-shop `access_token` (7-day) +
  `refresh_token` + `shop_cipher`, with HMAC-SHA256 signing of every request

`fetch_recent_orders` deliberately over-fetches, buffering ±6h so a show
crossing midnight is not sliced. One `--date` therefore routinely returns a
neighbouring show's orders. `fill_order_ids` keeps them (filling them costs
nothing) but reports counts per UTC capture date, so one uncovered date does
not look like a system-wide failure.

### `sku_name` vs `sku_id` — do not confuse these

- **`sku_name`** is the variant *display name*, which the seller sets. In this
  shop it is the bare item number, `"197"`. **This is what to match on.**
- **`sku_id`** is TikTok's internal 19-digit identifier. Useless for matching.

`parse_sku_part()` in `orders.py` reads `sku_name` for the item and
`product_name` for the Part. It lives in `orders.py` rather than a manifest
module so a cron job does not import reportlab to parse two integers.

### `lookup_screenshot` — the matcher

In `screenshots.py`. Given `(part, sku, order_time)` it returns a `Path`:

- buckets by the filename's UTC date, searching day offsets `-1, 0, +1`
  because a show straddles UTC midnight
- among candidates with the same `Part{X}_SKU{N}` key, picks the **closest
  timestamp**
- rejects anything further than `_MAX_MATCH_DELTA_SECONDS` (3 hours)

That rejection threshold is load-bearing. Item numbers recycle every show, so
the same Part+SKU exists on many dates. Verified in practice: an order for
`Part 1 Item #151` on 06-19 correctly returned `None` rather than attaching the
06-14 capture of the same number.

### `fill_order_ids.py`

```bash
make fill-orders ARGS="--date 2026-08-21 --dry-run"
make fill-orders ARGS="--since 1"
```

- `--date` is a **show date** in `SHOW_TZ`, matching `make run`
- `--since N` is a **range** — the last N days. It is relative to "today" in
  `SHOW_TZ`, so at 22:00 CDT on the 21st `--since 1` means the **20th**, not
  that evening's show. Run it the following morning, as the cron does
- `--dry-run` uses the identical code path; only the final PATCH is skipped
- Re-running is safe — already-correct rows are reported and left alone

> Note `main.py` uses **`--days-ago N`** for the same idea, deliberately not
> `--since`. There it means one specific date, not a range. The two are invoked
> from the same cron line, and one word with two meanings across adjacent tools
> is a trap that only surfaces when someone reaches for N > 1.

#### Exit code: failure, not gaps

Lines beginning `!` are things the run could not resolve. Most are routine and
**do not** affect the exit code. The `±6h` over-fetch always drags a
neighbouring show's rows into the PocketBase query without their orders being
fetched, so `no_order` is large on every single run — 896 on a night that
filled 1995 perfectly.

Exit 1 is reserved for the run not doing its job:

| Condition | Means |
|---|---|
| No order line items fetched at all | Auth or API broken, or genuinely no show |
| Orders fetched, **none** resolved to a screenshot | Broken index or broken match — a real show resolves essentially all |
| Screenshots resolved, PocketBase returned **no rows** | `source_file` not populated; matching perfect, nothing accomplished |
| A record already holds a **different** order id | A contradiction; one value is wrong and nothing can say which |

Every run ends with a fixed-shape line for log greps:

```
SUMMARY filled=1995 already=0 orders=1995 resolved=1995 rows=2891 \
        no_order=896 no_row=0 status=ok
```

`no_order` being large is normal. **`no_row` is the one worth investigating** —
a screenshot matched an order but no record carries that `source_file`, meaning
the watcher never ingested that capture.

---

## 10. Deployment

Production is an Ubuntu VPS running `TZ=UTC`. Both stacks run in Docker.

### Watcher stack

```bash
docker compose up -d --build      # pocketbase + watcher
```

PocketBase publishes on `127.0.0.1:8090` — **loopback only, deliberately**,
because the create rule is unauthenticated.

### Tracker stack

```bash
make build                        # rebuild after pulling
make run                          # interactive manifest (PDF/HTML)
make fill-orders ARGS="…"         # order-ID backfill
make nightly                      # unattended: manifest, then order IDs
make serve                        # nginx + file server for browsing manifests
```

`make build` **must** carry `--profile tools` or the `fill-orders` image is
never rebuilt — see trap 10.

`main.py` prompts for date, format and layout only when given no arguments.
`--date` or `--days-ago` suppresses every prompt, which is what makes `nightly`
schedulable: a cron job blocked on `input()` is indistinguishable from one that
hung.

`fill-orders` attaches to the watcher's Docker network
(`tiktok-screenshot-watcher_default`, declared `external`) and reaches
PocketBase as `pocketbase:8090` over the bridge — never through the published
loopback port, so the security property holds.

**Only `fill-orders` needs the watcher.** `make run` generates manifests
regardless. If the watcher was `docker compose stop`ped the network survives
and you get a connection refused; if it was `down`ed the network is gone and
the container will not start.

`fill_order_ids.py` can also run on the host — it needs only `requests` and
`python-dotenv`, not the PDF toolkit. Set `POCKETBASE_URL=http://127.0.0.1:8090`
and `TIKTOK_TOKEN_DIR=tokens`.

### Cron

```cron
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
CRON_TZ=America/Chicago

30 9 * * * cd /path/to/tiktok-order-tracker && \
  flock -n /tmp/nightly.lock make nightly >> logs/nightly-$(date +\%Y\%m).log 2>&1
```

The three environment lines are not decoration. `PATH` because cron's
environment omits `docker` and `make`; `CRON_TZ` because the VPS is UTC and a
bare `30 9` would fire at 04:30 Central; `flock` because a hand-run overlapping
the cron would have two processes refreshing the TikTok token at once, and
`auth.py:_save()` is an unlocked `write_text()` — a torn write there costs a
browser re-auth a headless VPS cannot perform.

Full walkthrough, including a test that runs under `env -i` so PATH problems
surface immediately, is in **`CRON_SETUP.txt`** in the watcher repo.
Deployment order and one-off migration steps are in **`DEPLOY_STEPS.txt`**.

`--since 3` re-checks earlier days and costs only API calls, since filled rows
are skipped. Useful for closing gaps left by a failed run.

---

## 11. Traps

Ordered roughly by how much damage they cause.

1. **`order_id` as a Number field** silently corrupts every ID. Text only.
2. **The `screenshot` field is not the filename.** PocketBase lowercases it and
   appends a random suffix. Match on `source_file`.
3. **`created` is ingest time, not capture time** — it can lag by hours during
   a backlog. For "when was this auctioned," use the date inside `source_file`.
4. **A manifest run moves screenshots** out of the watch folder
   (`archive_screenshots_for_date` copies then unlinks). If the watcher has not
   drained its queue, those items never reach PocketBase. Let it finish first.
   This also means no throwaway "test" manifest runs against live captures,
   even with `OUTPUT_DIR` pointed elsewhere.
5. **TikTok's `*_expire_in` is an absolute Unix timestamp, not a duration.**
   Treating it as a duration stored expiries in the year 2082, so the cache
   never looked stale, the refresh branch was unreachable, and the only
   recovery was deleting the token file and re-authorizing by browser every 7
   days. Fixed in `auth.py` via `_expiry_to_timestamp` and
   `_repair_legacy_expiries`.
6. **The `tools` profile changes behaviour twice, and both failures point
   somewhere else.** `fill-orders` sits behind it, and Docker Compose treats
   profiled services as absent unless the flag is passed:

   - **`docker compose run` without it** starts the service with an incomplete
     config, so the `.env` credentials never arrive. Surfaces as "missing
     TIKTOK_APP_KEY" — looks like a broken `.env`.
   - **`docker compose build` without it** skips the service entirely. Since
     each `build: .` service gets its own image, building `manifest` does not
     build `fill-orders`; its image stays whatever compose auto-built the
     first time it ran, and files added to the repo since are simply absent:

         python: can't open file '/app/backfill_source_file.py'

     after a pull that plainly contains the file. Looks like a failed pull.

   Both Make targets now carry the flag. The profile is still worth keeping —
   it stops `docker compose up` launching a one-shot job — but it is a sharper
   edge than it looks.
7. **Two capture machines writing to one folder** produce two rows per item and
   double the Gemini spend, and only one of each pair gets an `order_id` — so
   the gap report fills with false positives. Fix is settings-only: set the
   standby machine to `destination: s3` with a separate prefix so it never
   writes to the watcher's folder.
8. **`_iter_window` in `orders.py` has no retry** and never compares the
   yielded count to the `total_count` the API returns on every page. If
   pagination ever ends early, the shortfall is invisible and surfaces as "no
   screenshot found" — blaming the wrong subsystem. Reconciles cleanly in
   testing; worth adding a check before running unattended.
9. **PocketBase fails in opposite directions on read and write.** POSTing an
   unknown field returns **HTTP 200 and silently discards it**; *filtering* on
   an unknown field returns **HTTP 400**. So a missing column is invisible
   during ingest and loud during backfill. This is why `source_file` had to
   exist on the collection before the new watcher ran — otherwise a whole
   show's records are created looking fine, with the join key quietly absent.
10. **Do not narrow the filename pattern to one image format.** The watcher's
   `_maybe_submit` returns *before* its "wrong name" log line when the suffix
   does not match, so an unrecognised extension makes a whole show vanish with
   **no log output at all** — an idle-looking watcher and an empty database.
   This is why both `png` and `webp` are accepted.
11. **The token cache `_save()` is not atomic** (plain `write_text`, no lock).
   Host and container share `./tokens`, so a concurrent refresh could corrupt
   it. Narrow window; the watcher's `ProcessedLedger._flush()` shows the
   temp-file-and-rename pattern to copy.

---

## 12. Historical notes

- **The June 2026 archive is Eastern-named** (UTC−4), from before the UTC
  convention. Measured across 400 files: filename timestamps sit exactly 4
  hours behind file mtimes. Those screenshots no longer match and are
  **deliberately not backfilled**. Manifests already generated for those dates
  are unaffected and should not be regenerated — they would lose their photos.
- **2026-06-14 has 14 missing items** in Part 1. All were 12–27 seconds per
  missing item, i.e. the video-sync/logout failure, not a capture-timer drop.
- **06-19 and 06-20 have zero missing items** across 849 consecutive captures.
- **2026-08-22: `source_file` added and backfilled.** The field did not exist
  on production until then, so all 9,242 existing records had no join key —
  `fill_order_ids` matched 1995/1995 screenshots and filled nothing. A one-off
  script reconstructed the name from the stored `screenshot` value (see §4) and
  populated **9,176** of them; the remaining **66** are 2026-08-14 records
  whose screenshots are gone from disk and can never be matched. Some of those
  66 also have an empty `item_number`, from when it came from the model rather
  than the filename. 1,995 order IDs were filled the same day.
- **Extension v2.3 switched PNG → WebP q0.90.** v2 was producing 2.4–3.7 MB
  frames where v1 produced 300–500 KB; the label burned onto the frame made PNG
  compress poorly. Everything captured before v2.3 is PNG.

---

## 13. Open work

1. **Watcher reads S3** instead of the local folder. This is the main remaining
   piece. S3 has no inotify, so `watchdog` becomes prefix polling or bucket
   notifications. `ProcessedLedger` ports cleanly — it keys on basename+size,
   both of which `ListObjectsV2` returns per object.
2. **`fill_order_ids` needs an S3-aware screenshot index**, since
   `scan_screenshots` currently walks a local directory.
3. **Capture migration to the Texas warehouse machine.** The VPS browser loses
   video sync (blank frames) or gets logged out (no captures for a whole show).
   The warehouse host watches the same screen while running items, so a broken
   feed is noticed immediately. No Texas→VPS sync exists yet; the two capture
   streams do not meet, so there are no duplicates today.
4. **Duplicate-capture strategy** for the transition — deferred until something
   consumes S3. Preference is settings-only (separate prefixes, standby machine
   on `destination: s3`) over dedup code, because dedup would require giving
   the watcher superuser read access it does not currently need.
5. **Indexes** on `auction_items.source_file` and `order_id`. Not yet added,
   and `source_file` is now queried on every backfill run.
6. **`total_count` reconciliation** in `_iter_window` (trap 8). Reconciled
   cleanly whenever measured; the anomaly that prompted it turned out to be
   ordinary growth during a live show.
7. **Delete `backfill_source_file.py`** once its run is confirmed good. It is
   marked one-off in its own commit message; the condition it fixes cannot
   recur now that the watcher always writes the field.
8. **Nothing alerts on a failed nightly run.** Cron only emails on a non-zero
   exit if `MAILTO` and a mail transport are configured, which they are not.
   The `SUMMARY` line exists to be scraped by something; nothing scrapes it
   yet.

---

## 14. Quick verification recipes

```bash
# Is a filename parsed the way both repos expect?
python3 -c "import re; print(re.match(
  r'^Part(?P<part>\d+)_SKU(?P<sku>\d+)_(?P<date>\d{8})_(?P<time>\d{6})\.(?P<ext>png|webp)$',
  'Part1_SKU197_20260822_005141.webp').groupdict())"

# What is actually in PocketBase?
sqlite3 pb_data/data.db \
  "select item_number, source_file, order_id from auction_items limit 10;"

# Confirm the collection's API rules
sqlite3 pb_data/data.db "select quote(listRule), quote(createRule),
  quote(updateRule) from _collections where name='auction_items';"

# Which timezone was an archive captured in?
#   offset 0 = UTC, 4 = EDT, 5 = CDT
python3 -c "
import os,re,datetime,statistics
rx=re.compile(r'_(\d{8})_(\d{6})\.png$'); offs=[]
for n in os.listdir('output/images'):
    m=rx.search(n)
    if not m: continue
    named=datetime.datetime.strptime(m.group(1)+m.group(2),'%Y%m%d%H%M%S')
    mt=datetime.datetime.fromtimestamp(os.path.getmtime('output/images/'+n),datetime.UTC).replace(tzinfo=None)
    offs.append(round((mt-named).total_seconds()/3600))
print('median offset UTC+%d' % statistics.median(offs))"

# Dry-run the backfill without writing
make fill-orders ARGS="--date 2026-08-21 --dry-run"
```
