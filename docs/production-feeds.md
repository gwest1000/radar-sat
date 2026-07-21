# ECCC real-time production feeds

Radar-Sat uses ECCC's AMQPS notification service for systematic real-time acquisition. Sarracenia receives a notification as each product is published, downloads the announced file over HTTPS, and atomically renames it into a local raw spool. This avoids wasteful directory polling and follows ECCC's recommended Datamart access pattern.

The rendered BC composites and precipitation-type layers still come from GeoMet WMS because no equivalent gridded Datamart file is published. The raw feeds here supply the satellite, lightning, and four site-radar diagnostic loops.

## What is subscribed

| Config | Broker-side bindings | Client-side files retained | Expected cadence |
|---|---|---|---|
| `radarsat_goes_west` | `satellite.goes.west.#` | DayVis/NightIR, standalone 2-km NightIR, convective sandwich/night microphysics IR, snow-fog/night microphysics, natural colour | nominally 10 min; natural colour has a normal overnight gap |
| `radarsat_lightning` | `lightning.#` | `*_MSC_Lightning_2.5km.tif` | 10 min |
| `radarsat_bc_site_radar` | DPQPE and CAPPI bindings for `CASAG`, `CASHP`, `CASSS`, `CASPG` only | rain/snow DPQPE, contingency DPQPE, standard rain/snow CAPPI | 6 min |

The four radar stations are Aldergrove, Halfmoon Peak, Silver Star Mountain, and Prince George. Configuring each station as its own AMQP binding keeps national-radar traffic out of the queue.

Files land under:

```text
~/.local/share/radar-sat/spool/eccc/
├── satellite/
├── lightning/
└── radar/
```

The downloader writes a dot-prefixed temporary file and performs an atomic rename only after completion. Downstream ingest must ignore dotfiles. Raw files are staging inputs; only cropped/rendered web assets should be uploaded to R2.

## Installation on the ingest Mac

Deployment prerequisites are Python 3.11 or newer, outbound TLS access to `dd.weather.gc.ca` on AMQPS port 5671 and HTTPS port 443, a persistent logged-in macOS user session for the LaunchAgent, and at least 3 GB (5 GB recommended) of free local staging space when the recommended three-hour raw-spool window is used. No private ECCC credential is required.

Use a dedicated virtual environment in the final project directory:

```bash
cd /Users/greg/projects/radar-sat
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-feeds.txt
chmod +x scripts/manage_eccc_feeds.sh
```

For the automated bring-up, `scripts/ops/setup_local.zsh` refuses the macOS system Python 3.9, prefers `/opt/homebrew/bin/python3.12`, and checks other Python 3.11+ candidates if needed. Set `RADARSAT_BOOTSTRAP_PYTHON` to an explicit compatible interpreter on a different host. An existing virtual environment is validated in place and is never moved.

`metpx-sr3[amqp,reassemble]` is pinned to `3.2.0.post1`, the current stable PyPI release at implementation time. The AMQP extra is mandatory and the reassembly extra safely handles any object announced in multiple blocks; `sr3 features` must report both capabilities as installed.

The repository invokes sr3 through `scripts/sr3-radarsat`, a narrow wrapper
that supplies a stable local queue hostname when macOS `.local` reverse DNS is
unavailable. Named remote DNS lookups are unchanged. It also works around two
3.2.0/macOS process-manager incompatibilities: Homebrew's capitalized `Python`
process name plus unavailable full-memory counters, and the `sanity` action
being forwarded into a child before its valid `start` action. These shims are
scoped to sr3 process discovery and `instance.py` children. Set
`RADARSAT_SR3_HOSTNAME` only when a deliberate, stable queue suffix is needed.

Install the three repository configs into Sarracenia's per-user config directory and validate both the local invariants and Sarracenia's own parser:

```bash
./scripts/manage_eccc_feeds.sh check
```

The helper writes the public ECCC anonymous credential only to Sarracenia's platform config directory with mode `0600`; committed configs contain no password. On macOS this is `~/Library/Application Support/sr3/credentials.conf` (on Linux it is normally `~/.config/sr3/credentials.conf`). It also creates the spool directories. Set `SR3_CONFIG_DIR` only if the Sarracenia installation was deliberately configured to use a different root.

## First live validation

Start one low-volume feed interactively:

```bash
./scripts/manage_eccc_feeds.sh foreground lightning
```

After the first complete file arrives, stop it with `Ctrl-C`, start all feeds, and check source-time freshness and file signatures:

```bash
./scripts/manage_eccc_feeds.sh start
./scripts/manage_eccc_feeds.sh status
python3 scripts/check_eccc_feeds.py --require-data
```

Freshness gates are 45 minutes for each continuous satellite product, 35 minutes for lightning, and 20 minutes for each DPQPE and CAPPI stream at each site. Natural colour is monitored as optional because its overnight gap is normal. These are monitoring limits, not guarantees. The validator uses source timestamps in filenames rather than local modification time and verifies TIFF/GIF magic bytes.

## Renderer integration and fallback

The normal ingest command consumes the spool automatically:

```bash
PYTHONPATH=. .venv/bin/python scripts/run_ingest.py \
  --output-root data/output --domain bc --hours 3 \
  --spool-root "$HOME/.local/share/radar-sat/spool/eccc" \
  --spool-mode auto --spool-hours 12
```

The native backlog window is deliberately independent of `--hours`: scheduled
GeoMet discovery remains three hours, while `--spool-hours 12` matches the
broker queue/retry window. After a workstation or network outage, all safe
native files from that recovery window are rendered before the raw pruner runs.
Lightning age trails normally retain the six-minute composite clock; where the
local composite archive has an outage gap, recovered ten-minute lightning times
become explicit trail anchors rather than being left as unused source frames.

`--spool-mode` has three explicit behaviours:

- `auto` (production default) renders native BC satellite and lightning files first, then asks GeoMet only for still-missing times. A same-time WMS bootstrap frame is replaced when its native file arrives.
- `off` ignores the raw spool and uses the existing GeoMet bootstrap path. This is useful for a demo host that does not run Sarracenia.
- `only` disables WMS fallback for the five native-capable BC layers. It **does not** disable GeoMet radar rain/snow composites, precipitation type, or either coverage mask; those proven gridded paths are always retained.

The native path is intentionally limited to the BC grid in this first production version. Each GOES RGB GeoTIFF is read in its declared geostationary CRS, bilinearly reprojected and cropped to the 1920×1472 EPSG:3005 grid, then encoded as quality-88 WebP. The one-band lightning density GeoTIFF is nearest-neighbour reprojected so isolated positive cells are not diluted; positive density bins are rendered opaque for the downstream categorical age-trail glyphs, while zero and nodata are transparent. North America and North Pacific remain phase-two domains and are not advertised as available products in the initial catalog.

The `site-radar` layer is a 2×2 PNG montage of DPQPE **rain** GIFs from CASAG, CASHP, CASSS, and CASPG. A frame is emitted only when all four sites have the exact same source timestamp. Primary imagery wins when primary and contingency files coexist; a same-time contingency image is accepted when primary is absent and is labelled in the panel and metadata. A newer scan from only one to three sites never creates a mixed-time frame. Snow DPQPE and CAPPI files remain available in the raw spool for future dedicated products but are not silently mixed into this display.

Only recognized, regular, non-symlink TIFF/GIF files below the three exact feed directories are considered. Any dot-prefixed file or file below a dot-prefixed directory is treated as Sarracenia inflight state and ignored. The consumer also checks size bounds, magic bytes, raster readability/CRS, and agreement between the filename time and a GeoTIFF validity tag. Bad inputs are skipped and listed under `spool.domains.bc.rejected` in `status/ingest.json`; in `auto` mode GeoMet can still fill a rejected satellite/lightning time. Their basenames are also recorded in `preserveFiles`, excluded from the scheduled raw prune, and exposed as an ingest health error until an operator resolves them.

The consumer is read-only and idempotent with respect to the raw spool: it never moves, renames, or deletes feed files. Rendered metadata uses the standard `validTime`, `path`, `source`, `sourceLayer`, and `fetchedAt` fields, records `source: ECCC Datamart`, and adds source filenames and actual source times. The catalog discovers these frames through the same metadata tree as every other layer.

The scheduled cycle accepts `RADARSAT_SPOOL_ROOT`, `RADARSAT_SPOOL_MODE`, and `RADARSAT_SPOOL_INGEST_HOURS`; their defaults match the command above. It runs the apply-form spool pruner only after ingest/render succeeds, using `RADARSAT_RAW_RETENTION_HOURS` (default three hours), and before publication. Backlog files delivered after an outage have fresh local modification times, but are still selected by their source time within the separate 12-hour ingest window. A PID-owned cycle lock prevents overlap; a live owner is left alone and a lock whose PID is no longer running is safely isolated and recovered on the next cycle.

For boot persistence on macOS, install the supplied per-user launch agent:

```bash
./scripts/manage_eccc_feeds.sh install-agent
launchctl print gui/$(id -u)/ca.radarsat.eccc-feeds
```

The launch agent runs a small supervisor, checks the three subscribers every minute with `sr3 sanity`, and writes wrapper logs to `~/Library/Logs/Radar-Sat/`. On macOS, Sarracenia's detailed transfer logs remain under `~/Library/Caches/sr3/log/` (normally `~/.cache/sr3/log/` on Linux).

## Queue and recovery choices

- Three independent queues keep large satellite downloads from delaying radar or lightning.
- Queues expire after 12 hours of disconnection. Retry and maximum accepted message ages are also 12 hours.
- The subscriber uses one-message prefetch and two workers for satellite/radar, one for lightning.
- Static queue names include component, config, and host, as recommended by ECCC for anonymous users.
- The precise bindings remain comfortably below ECCC's published 10,000-message anonymous-queue guidance during a 12-hour outage.
- `overwrite False` prevents an already-present file with the matching advertised checksum from being downloaded again.

Changing bindings on an existing config adds bindings to the existing broker queue. Before deploying a binding change, explicitly clean up the old queues and then restart:

```bash
./scripts/manage_eccc_feeds.sh cleanup
./scripts/manage_eccc_feeds.sh start
```

`cleanup` removes the Radar-Sat queues and bindings from the broker; use it only during a planned stop or binding migration.

## Raw-spool disk bound

The selected 1-km GOES files dominate local staging space and bandwidth. Four products can total roughly 0.3–0.6 GB per hour depending on scene compression, while lightning and site-radar GIFs are comparatively small. Keep only a short recovery window in the raw spool; processed imagery has its own 24-hour/7-day retention policy.

Review a three-hour prune first, then apply it after a successful renderer cycle has consumed the files:

```bash
python3 scripts/prune_eccc_spool.py --older-than-hours 3
python3 scripts/prune_eccc_spool.py --older-than-hours 3 --apply
```

The pruner only considers non-dot `.tif`/`.gif` files below the three exact feed directories, refuses broad filesystem roots, never follows symlinks, always preserves the newest file in each feed, and consumes ingest status so rejected sources are retained for retry. Rendering is idempotent, so retaining or replaying a raw file does not duplicate catalog frames.

## Monitoring and incident response

1. Run `scripts/manage_eccc_feeds.sh status`. A stopped/missing process is local; run `sanity` or restart it.
2. Run `scripts/check_eccc_feeds.py --require-data`. If one family is stale while the others are fresh, inspect its Sarracenia log and the corresponding ECCC directory.
3. Confirm that the absence is not expected: natural colour is daylight-only, and a single radar station may be in maintenance while ECCC publishes a `-Contingency` image.
4. If all three feeds stop together, check broker reachability and ECCC service notices before changing configuration.
5. If a queue was idle longer than its expiry, restart and use the Datamart's dated HTTPS tree for a bounded one-time backfill. Do not poll the real-time tree continuously.

The primary Datamart is described by ECCC as operational 24/7. `hpfx.collab.science.gc.ca` is an alternate high-bandwidth distribution host and broker, but ECCC explicitly notes that its Internet link does not have 24/7 redundancy. It is therefore a useful incident fallback, not an equal always-on primary. HPFX routing keys include the additional `*.WXO-DD.` prefix; do not simply replace the hostname in these configs without updating the bindings.

## Authoritative references

- [ECCC AMQP access and anonymous-user guidance](https://eccc-msc.github.io/open-data/msc-datamart/amqp_en/)
- [ECCC MSC Datamart access, retention, and HPFX fallback](https://eccc-msc.github.io/open-data/msc-datamart/readme_en/)
- [ECCC GOES Datamart products and filename rules](https://eccc-msc.github.io/open-data/msc-data/obs_satellite/readme_satellite-datamart_en/)
- [ECCC lightning Datamart path and filename rules](https://eccc-msc.github.io/open-data/msc-data/lightning/readme_lightning-datamart_en/)
- [ECCC radar DPQPE/CAPPI paths, filenames, and contingency products](https://eccc-msc.github.io/open-data/msc-data/obs_radar/readme_radarimage-datamart_en/)
- [Current Sarracenia subscriber guide](https://metpx.github.io/sarracenia/How2Guides/subscriber.html)
- [Current Sarracenia installation guide](https://metpx.github.io/sarracenia/Tutorials/Install.html)
- [Sarracenia configuration-option reference](https://metpx.github.io/sarracenia/Reference/sr3_options.7.html)
