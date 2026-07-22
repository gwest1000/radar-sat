# ECCC real-time production feeds

Radar-Sat uses ECCC's AMQPS notification service for systematic real-time acquisition. Sarracenia receives a notification as each product is published, downloads the announced file over HTTPS, and atomically renames it into a local raw spool. Separate NOAA public-S3 paths read half-hourly calibrated GOES-18/19 ABI and Himawari-9 AHI imagery plus ten-minute GOES-18 smoke and total-lightning products.

The rendered BC composites and precipitation-type layers still come from GeoMet WMS because no equivalent gridded Datamart file is published. The raw feeds here supply the satellite, lightning, and four site-radar diagnostic loops.

## What is subscribed

| Config | Broker-side bindings | Client-side files retained | Expected cadence |
|---|---|---|---|
| `radarsat_goes_west` | `*.WXO-DD.satellite.goes.west.#` | DayVis/NightIR, standalone 2-km NightIR, convective sandwich/night microphysics IR, snow-fog/night microphysics, natural colour | nominally 10 min; natural colour has a normal overnight gap |
| `radarsat_lightning` | `*.WXO-DD.lightning.#` | `*_MSC_Lightning_2.5km.tif` | 10 min |
| `radarsat_bc_site_radar` | DPQPE and CAPPI bindings for `CASAG`, `CASHP`, `CASSS`, `CASPG` only | rain/snow DPQPE, contingency DPQPE, standard rain/snow CAPPI | 6 min |

The four radar stations are Aldergrove, Halfmoon Peak, Silver Star Mountain, and Prince George. Configuring each station as its own AMQP binding keeps national-radar traffic out of the queue.

ECCC exposes `/today/` as the convenient current-data HTTPS alias, but live
AMQP announcements use the dated source path. A key observed during deployment
began `v02.post.20260721.WXO-DD.`. With `topicPrefix v02.post`, the leading `*`
matches the single `YYYYMMDD` component and `WXO-DD` selects the Datamart
source. Omitting both components creates a valid but permanently silent binding.

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

The ECCC native-spool path is limited to the BC grid. Each GOES RGB GeoTIFF is read in its declared geostationary CRS, bilinearly reprojected and cropped to the 1920×1472 EPSG:3005 grid, then encoded as quality-88 WebP. The one-band lightning density GeoTIFF is nearest-neighbour reprojected so isolated positive cells are not diluted; positive density bins are rendered opaque for the downstream categorical age-trail glyphs, while zero and nodata are transparent.

The parallel NOAA path reads calibrated Level-2 GOES multiband files and the northern Himawari full-disk segments with Satpy. GOES-18/19 are blended for North America; Himawari-9/GOES-18 are blended on the Pacific-centred EPSG:3832 grid. Raw true colour and a configured 10.3 µm brightness-temperature enhancement are offered separately. The additional `raw-visir` layer uses true colour above +8° solar elevation, neutral grayscale 10.3/10.4 µm IR below −6°, and a smoothstep blend through twilight. It never mixes the false-colour IR enhancement through the terminator, and it separately suppresses low-sun chroma from +1° to +12° so the source RGB cannot leave a red/yellow rim. A conservative RGB-distribution match is feathered only through the GOES-18/19 overlap to reduce the chromatic seam while leaving uncontested imagery unchanged. Downloads are processed one satellite at a time, may not exceed `RADARSAT_RAW_SAT_MAX_BYTES` (900 MB by default), and are deleted in a `finally` block. Only WebP display rasters persist. If a secondary satellite fails, the primary satellite is retained for that cycle and the degraded blend is recorded as an ingest warning.

Existing archives can be populated without another ABI/AHI download:

```bash
PYTHONPATH=. .venv/bin/python scripts/derive_raw_visir.py \
  --output-root data/output --domain north-america
```

The backfill pairs frames by their unchanged validity timestamp, reconstructs
an approximate monotonic neutral IR ramp from the known legacy enhancement,
and writes only `raw-visir` WebP/metadata files. It does not modify the existing
`raw-visible` or `raw-ir` bytes or metadata. The normal ingest path performs
the same local check for the newest timestamp before considering a new raw
download. `raw-visir` follows the same tier-based retention policy as its pair.
Small dashed arcs along the far-northern geostationary scan edge are inherited
no-data gaps, not meteorological features. `raw-visir` falls back to neutral IR
where only visible is missing and uses transparency where both channels are
unavailable, allowing the map beneath to show through without invented pixels.

The lightweight GOES-18 hazard path runs independently of that half-hourly
imagery clock. For each cycle it selects the latest ABI Aerosol Detection
Product (`ABI-L2-ADPF`) and the latest complete set of thirty 20-second GLM
Lightning Cluster Filter Algorithm files (`GLM-L2-LCFA`) in a ten-minute
window. It produces these transparent PNG layers:

- `smoke`: medium- and high-confidence daytime, clear-sky ADP detections. The
  pale tint is a confidence overlay, not an estimate of concentration or proof
  that transparent pixels are smoke-free.
- `glm-lightning`: quality-controlled optical total-lightning flash centroids
  collapsed to roughly 10 km display bins. These are not ground-strike
  locations, and useful GOES-18 GLM coverage is limited to about 52°N.
- `glm-lightning-trail`: bolt glyphs derived from the current, 10-minute-old,
  and 20-minute-old bins, with older flashes fading in colour.

Every downloaded ADPF or GLM source object is capped independently by
`RADARSAT_GOES_HAZARD_MAX_BYTES` (100 MB by default), decoded, and deleted
immediately. Only the processed PNG and JSON metadata archive persists. Set
`RADARSAT_GOES_HAZARDS_ENABLED=0` to disable this path. The separate ECCC CLDN
density layer remains the northern-coverage source on the BC grid; a single
masked GLM/CLDN hybrid layer for broad domains is not yet generated.

At the first production sample, the compressed display pairs averaged 0.32 MB
for BC, 0.56 MB for North America and 0.63 MB for the North Pacific. The
retention policy admits at most 336 half-hour BC raw times and 192 broad times
per domain. Allowing roughly twice the nighttime sample size for brighter
daytime visible imagery gives a conservative raw-satellite archive allowance
of about 0.7 GB. Broad radar plus coverage masks are only tens of kilobytes per
time. These layers therefore remain well inside the existing 4 GB warning and
5 GB hard publication guards; they do not rely on the account's nominal 10 GB
ceiling for safety.

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
