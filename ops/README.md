# Radar-Sat operations

Independent three-minute full-disk and five-minute-BC satellite workers, a
five-minute observation worker, an independent half-hour model-contour worker,
and a half-hour Pacific archive worker each use PID locks. Each completed run
atomically rebuilds `catalog.json`, publishes its
referenced assets through the shared R2 lock, commits the complete catalog, then
commits the compact `catalog-index.json` discovery document last. Browsers poll
the index and fetch the complete image history only for a compatibility or
failure fallback.
The publisher uses macOS `lockf` for an OS-held, FIFO-ordered lock; process exit
releases it automatically and stale-lock recovery cannot admit two publishers.
Raw pruning runs only after observation rendering; any rejected source files
are explicitly preserved for retry and surfaced by health checks. Expired remote
objects are deleted only after the catalog commit and only when their timestamps
independently violate the local retention policy. A 9-day R2 lifecycle rule is
the final backstop.

## Low-latency edge

Lightning and radar do not wait for the monolithic observation catalog. The
`lightning-edge` agent runs each minute, discovers the ECCC spool once for all
three domains, and renders a rolling three-file (about one-minute) GOES-18 GLM
batch. The `radar-edge` agent checks the ECCC GeoMet composite every two
minutes. Both publish only changed transparent rasters followed by the tiny
`live-edge.json` commit pointer. The browser polls that pointer every 30
seconds and uses it only for the newest frame; historical playback remains
time-matched to the immutable video manifest.

The radar worker retrieves each domain-independent GeoMet timeline once per
cycle, then renders the three domains in parallel. Connection retries use a
short connect timeout and jitter, while map failures are isolated by layer and
domain. A successful BC frame is therefore still published if a simultaneous
North America coverage request fails. Total GeoMet failure keeps the last-good
radar and still runs the South Coast NEXRAD derivation/publication before the
worker reports a non-zero status for monitoring. This is resilience around the
free WMS service, not a second Canadian-composite source: the public Datamart
radar feed contains station GIF imagery, while the downloadable 1-km composite
is a cost-recovered product.

Live and archive video are also separate jobs and locks. The ten-minute live
job cannot be blocked by the low-priority hourly archive encoder. Ordinary
fast publications expose a 24-hour recovery catalog, so all 6-, 12-, and
24-hour choices remain available even if video decoding is unsupported.

HRDPS and ECMWF model contours have their own `model-contours` agent and lock.
It refreshes and publishes every 30 minutes without waiting for the slow North
Pacific satellite blend, so satellite lock contention cannot make the model
overlays age out of the operational display. Scheduled runs prioritize the
newest valid hour and let the archive accumulate naturally; set
`RADARSAT_MODEL_CONTOUR_RECOVERY_HOURS` only when an explicit historical repair
is needed. The worker can wait up to 15 minutes behind an in-progress atomic R2
reconciliation instead of discarding a completed render at the generic
five-minute publisher timeout.

## Display-resolution H.264 loops

The video path uses exact, fully composited H.264 loops for the two common
operational stacks. A reusable pilot core can instead bake the expensive lower
part of the stack and add a small number of immutable transparent overlays in
the browser. Unsupported combinations retain the satellite-video/proxy or
lossless-image fallback.

Install ffmpeg with libx264, then enable the video renderer without reinstalling the
LaunchAgent:

```text
RADARSAT_VIDEO_ENABLED=1
RADARSAT_VIDEO_LIVE_HOURS=24
RADARSAT_VIDEO_ARCHIVE_HOURS=168
# RADARSAT_FFMPEG=/opt/homebrew/bin/ffmpeg
```

For the two configured operational layer stacks, the 3-, 6-, 12- and 24-hour
loops are exact-range progressive MP4s with one variable-duration H.264 sample
per weather frame. The browser uses native looping and performs no JavaScript
seek at the boundary. BC products publish only their full-quality,
approximately 1920-pixel rendition; broad products publish only their useful
display-scale rendition. New high-only generations let the duplicate
1280-pixel BC assets age out under normal retention.

BC XL, Northeast BC and North America also pilot a reusable
`weather-smoke-core-v1` prefix. It bakes satellite, enhanced smoke, radar and
static geography into H.264, then draws lightning, fires and the combined
MSLP/500-hPa contour overlay from the same immutable generation. Exact full-loop
matches always take priority. The hybrid core is built explicitly in a
lower-priority scheduler lane and therefore cannot delay exact operational
loops. Seven-day and unsupported layer combinations retain the HLS or lossless
image fallback until the later CMAF and hybrid-core expansion.

One bounded video scheduler serializes live, day and archive media work so the
jobs cannot contend for the same source tree or delete one another's inputs. It
builds exact ranges first, using at most two product workers in parallel on the
Mac, publishes each urgent range batch, then considers one lower-priority
hybrid or archive unit. It refreshes MSC GeoColor for the BC family, NOAA VIS/IR
for North America, and NOAA VIS/IR for the two Pacific products. A separate
one-minute MSC edge job renders and publishes only the newest Datamart GeoColor
frame, so the live edge is not held behind video encoding. ECCC DayVis/NightIR
and standalone NightIR are no longer subscribed, rendered or exposed; NOAA
remains the IR-only choice.

The browser intentionally buffers complete live tracks because a 24-hour
weather loop is only about 25–33 seconds of encoded playback and must remain
smooth at 4×. Archive tracks are two to four minutes long, so hls.js targets a
45-second forward buffer with a 60-second ceiling, a 48 MiB byte budget, and a
15-second back buffer. Immutable segments already visited remain in the normal
HTTP cache for later seeks and loop passes. Prepared overlay surfaces use an
adaptive decoded-memory budget, allowing a six-hour loop—and, on an 8 GB+
device, normally the full 24-hour loop—to avoid rebuilding overlays after its
first circuit.

The publisher always protects the current catalog's media and proxy objects
plus one immediately previous manifest for an atomic browser handoff.
Unreferenced rolling exact MP4s receive a fifteen-minute transition grace and
are then removed. Content-addressed proxy and
static-overlay prefixes deliberately have no blind R2 age lifecycle: boundary,
watershed, transmission-line, or other unchanged proxies may remain referenced
for longer than nine days. The publisher's post-commit reachability cleanup
removes those objects once they are no longer referenced.

## Credentials

Use a Cloudflare R2 object token scoped to the `radar-sat` bucket with Object Read
and Write permission. Environment variables take precedence. On macOS, the
publisher also reads these Keychain generic-password services with account
`radar-sat`:

- `radar-sat-r2-account-id`
- `radar-sat-r2-access-key-id`
- `radar-sat-r2-secret-access-key`
- `radar-sat-r2-bucket`
- `radar-sat-r2-public-base-url`

Account ID, bucket, and public URL may instead live in `.env`; do not put the
secret access key in a committed file.

After revoking any exposed token, create a fresh bucket-scoped Object Read &
Write token and store its S3 access-key pair without echoing the secret:

```bash
scripts/ops/store_r2_credentials.zsh
```

## Bring-up

```bash
scripts/ops/setup_local.zsh
scripts/ops/store_r2_credentials.zsh
PYTHONPATH=. .venv/bin/python scripts/run_ingest.py --domain bc --hours 168
PYTHONPATH=. .venv/bin/python scripts/publish_r2.py --dry-run
PYTHONPATH=. .venv/bin/python scripts/publish_r2.py
scripts/ops/install_launchd.zsh
```

Pass one or more agent names to update only those jobs, for example:

```bash
scripts/ops/install_launchd.zsh msc-satellite-edge lightning-edge radar-edge model-contours video-day video-archive
```

The live video job retains the native 10/20-minute cadence for 3-, 6-, and
12-hour loops. The independent `video-day` job builds the 24-hour loop every
30 minutes, while `video-archive` retains the hourly seven-day loop. Separate
jobs keep the longer products from delaying the operational live refresh.

The production bucket already has site CORS and a nine-day `frames/` lifecycle
backstop. Bucket configuration is a one-time control-plane operation. If the
bucket is recreated—or the optional metadata/one-day multipart rules from
`scripts/configure_r2.py` are applied—use the Cloudflare dashboard or a
separate, short-lived administrative token, then revoke it. Do not broaden the
long-lived Object Read & Write publisher token; it intentionally cannot change
bucket configuration.

The publisher warns at 9.0 GB and refuses growth beyond 9.8 GB by default,
retaining an explicit margin below the 10 GB free-storage allowance. It lists
the dedicated bucket before every commit so the guard includes orphaned and
out-of-band objects, not just the local archive.

Health state is written to `var/status/health.json`; ingest and publication state
are in `${PROJECT_DATA_ROOT}/radar-sat/data/output/status/ingest.json` and
`var/status/publish.json`. Without a shared root, the output path falls back to
`data/output`. Run the
checker directly with:

```bash
PYTHONPATH=. .venv/bin/python scripts/check_health.py
```

`RADARSAT_SPOOL_ROOT` defaults to
`$HOME/.local/share/radar-sat/spool/eccc`; `RADARSAT_SPOOL_MODE` defaults to
`auto`. Use `off` for a WMS-only bootstrap host or `only` to suppress WMS
fallback for native-capable BC satellite/lightning layers. Composite radar,
precipitation type, and their coverage masks remain on GeoMet in every mode.
`RADARSAT_RAW_RETENTION_HOURS` controls the scheduled raw-spool prune and
defaults to three hours. The cycle lock records its owning PID: a second live
cycle exits cleanly, while a dead owner's stale lock is recovered automatically.
`RADARSAT_SPOOL_INGEST_HOURS` defaults to 12 hours, matching the broker recovery
window independently of the three-hour GeoMet query. Thus an outage backlog is
rendered before its raw staging files are eligible for deletion.

The separate genuine ten-minute GOES-18 WestWX path is installed in the same
locked cycle but disabled by default because it transfers roughly 36–38 GB of
compressed NOAA input per full day. First run the one-scan benchmark documented
in `docs/westwx-satellite.md`. To activate the scheduled isolated catch-up, set
this in `.env`; no LaunchAgent reinstall is required:

```text
RADARSAT_WESTWX_SATELLITE_ENABLED=1
```

Each cycle processes at most one missing newest-first frame with a 0.4 GB
download cap and a separate cache. A failure writes WestWX status and warns but
does not prevent normal Forecast Graphics publication. The environment knobs
`RADARSAT_WESTWX_SATELLITE_HOURS`,
`RADARSAT_WESTWX_SATELLITE_MAX_DOWNLOAD_GB`, and
`RADARSAT_WESTWX_SATELLITE_MAX_SOURCE_MB` may tighten the defaults; do not widen
them without reviewing a dry-run plan.

When the ten-minute path is enabled, the southern-BC PACUS path defaults on as
well. Its separate worker processes one roughly 53 MB file per run, deletes the
raw file after rendering, and retains only the compact `raw-visir-5min` finals
for 24 hours. The observed final is about 0.61 MB, or roughly 0.18 GB for all
288 five-minute frames. PACUS ends near 53.5°N; the renderer uses the newest
ten-minute full-disk frame farther north and feathers inward from the curved
scan edge so the footprint is not drawn across the map. It can be controlled
independently with:

```text
RADARSAT_FIVE_MINUTE_BC_SATELLITE_ENABLED=1
RADARSAT_FIVE_MINUTE_BC_SATELLITE_MAX_FRAMES=1
RADARSAT_FIVE_MINUTE_BC_SATELLITE_MAX_DOWNLOAD_GB=0.15
```
