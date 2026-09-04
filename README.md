# BC Satellite/Radar/Lightning/Fires

Operational satellite, radar, precipitation-type, and lightning loops for
British Columbia and its upstream weather. The viewer uses one set of aligned
source layers to build several products in the browser, so a new overlay does
not duplicate imagery in R2.

Public site: <https://gwest1000.github.io/radar-sat/>

## Launch products

- **Overlay:** BC XL, tightly cropped BC, southwest, southeast, northeast, and
  South Coast
  views. Each provides mutually exclusive NOAA VIS/IR and IR plus ECCC VIS/IR,
  IR and convective-sandwich satellite backgrounds; mutually exclusive
  radar/precipitation-type overlays; and independent lightning and wildfire
  hotspots.
- **Snow / fog:** BC Small, southwest, southeast and northeast qualitative RGB
  loops.
- **North America:** configurable ten-minute GOES-18 true colour, day/night
  VisIR or enhanced IR, with the ECCC continental radar composite and honest
  coverage hatching.
- **North Pacific:** Himawari-9/GOES-18 true colour or enhanced IR on a
  dateline-safe grid, with the real West Coast radar footprint.

The BC, southwest and southeast displays can use genuine five-minute GOES-18
PACUS imagery south of its scan edge with ten-minute full-disk imagery farther
north, but every BC product is presented on one consistent ten-minute loop
clock. Pacific/WNA, North America and Pacific use a twenty-minute loop clock.
Selecting seven days switches every product to a consistent hourly clock; its
lightning layer aggregates the six preceding ten-minute detection bins. The
rapid-source finals are retained for 24 hours, while the standard source feeds
all BC views through the seven-day archive. All regional displays crop the
shared aligned BC grid, gaining on-screen detail without storing duplicate
regional rasters. The watershed overlay uses
the same 54-polygon BC Hydro boundary source as the forecast-model plots.

Every map shows the real source timestamps. Old data is never silently relabelled
as current, and hatched grey means no current radar coverage rather than no echo.
The South Coast radar keeps that one-kilometre ECCC field as its complete base
and overlays NOAA's public KATX and KLGX dual-polarization instantaneous
precipitation rates. The U.S. insert has 250-m range bins and one-degree
radials, uses the same mm/h colour scale, and never erases the ECCC field when
a U.S. beam is blocked or a Level III object is briefly unavailable.
Lightning density cells are rendered as white-ringed flash markers that fade
with age rather than opaque grid squares.
Filled coral flames are agency-reported active fires, with larger symbols only
for official BCWS Wildfires of Note and current U.S. ICS-209 large incidents.
Hollow, age-fading coral flames are archived ten-minute snapshots of NRCan
CWFIS satellite thermal detections; they are not fire perimeters or confirmation
of an active wildfire.

Optional HRDPS Continental 2.5-km overlays add hourly 500-hPa height contours
(6 dam) and MSLP contours (4 hPa). The renderer reuses fields already required
by fcstGraphics and downloads only the two intermediate hourly GRIBs. High/low
centres use smoothed neighborhood extrema, broad-background prominence and
physical-distance suppression so weak gridscale extrema are not labelled.

## Runtime storage

Machine-level storage is configured once in `~/.config/project-data.env`:

```text
PROJECT_DATA_ROOT=/Volumes/Greg1_2tb/project-data
```

Radar-Sat uses `${PROJECT_DATA_ROOT}/radar-sat/data/output`. The
project-specific `RADARSAT_DATA_ROOT` and `RADARSAT_OUTPUT_ROOT` variables take
precedence. Without a configured root, development commands fall back to
`data/output`; a configured but unavailable root fails immediately so a missing
SSD cannot redirect production output to internal storage.

The browser checks a compact catalog index every minute with an ETag and only
parses a new index when its generation changes. The index keeps the newest
ordinary frame per layer plus the current video pointers; the complete image
catalog is fetched only if video is unavailable or fails. The page does not
hard-reload, so warmed media and overlay caches survive. A visible window keeps
looping when another window or application has focus; only a genuinely hidden
tab pauses playback.

Radar and lightning also have a separate `live-edge.json` path. It is updated
independently of catalog pruning and video encoding, and replaces the complete
newest display frame atomically when those observations are newer than the
historical H.264 loop. GOES-18 GLM uses a rolling one-minute batch south of
52°N; the ECCC density product supplements it across the rest of Canada.

## Architecture

```text
ECCC Datamart AMQPS ── GOES / lightning / site radar ┐
ECCC GeoMet WMS ────── composite / ptype / coverage ├─ local render + retention
NOAA public S3 ──────── GOES/AHI + GLM/ADP + NEXRAD ┤
                                                     └─ R2 layers + catalog index/full fallback
                                                                  │
GitHub Pages static viewer ◀──────────────────────────────────────┘
```

- BC grid: EPSG:3005, 1920×1472, approximately 145–108°W and 45–63°N.
- BC presentation: ten-minute frames through 24 hours; selecting seven days
  uses hourly frames for the complete loop.
- Five-minute BC PACUS finals require about 0.18 GB for a full 24-hour archive
  at the observed 0.61 MB/frame. Source transfer is about 15 GB/day, but the
  53 MB working file is deleted after each render.
- North America and Pacific presentation: 20 minutes through 24 hours, then
  hourly for the complete seven-day loop. The Himawari-9/GOES-18 North Pacific
  blend remains 30 minutes for 24 hours, then hourly; GOES-18 smoke and total
  lightning use a 10-minute clock.
- The ECCC 1 km North American radar composite is retained at its genuine
  six-minute clock for 24 hours, then hourly through day 7.
- The viewer uses server-rendered transparent lightning-trail PNGs (normally
  6–12 KB) instead of rebuilding hundreds of flash symbols in the browser.
- Display-resolution H.264/HLS serves the default satellite background in each
  domain and any other satellite choice that has a current complete profile. A
  hardware video decoder supplies the satellite clock while prepared,
  display-sized overlay surfaces swap atomically over it. The image renderer and
  full catalog remain the automatic compatibility and failure fallback. Live
  loops retain their complete 25–33 seconds of encoded media for smooth 4×
  replay; longer seven-day tracks use a 45-second target and 60-second hard
  forward-buffer ceiling instead of retaining most of the archive in
  MediaSource.
- Dynamic clients can use `glm-lightning-points` and `hotspot-points` instead
  of the legacy symbol PNGs. Each compact JSON frame uses normalized top-left
  coordinates and tuple schemas `[x,y,ageMinutes,count]` for GLM or
  `[x,y,ageMinutes,frp,count]` for CWFIS. Fresh GLM ages are referenced to the
  ten-minute window end (20-second precision); fresh hotspot ages use the exact
  CWFIS detection timestamp.
- `lightning-points` exposes the ECCC/CLDN ten-minute density fallback with the
  same `[x,y,ageMinutes,count]` tuple. Here `count` honestly means connected
  positive 2.5-km density cells rather than individual flashes or strokes, and
  the five-minute age is the midpoint estimate for the source window.
- `raw-visir` is a server-rendered true-colour day / neutral 10.3–10.4 µm IR
  night image. A solar-elevation smoothstep removes the false-colour terminator
  fringe, low-sun chroma is faded separately, and a bounded overlap correction
  softens the GOES-18/19 colour seam.
- Geostationary scan-edge pixels missing from both visible and infrared are
  transparent; the renderer does not synthesize weather into those gaps.
- Raw NOAA source scans are handled one at a time under a 900 MB hard cache
  cap for multiband imagery or a 100 MB per-object hazard cap, then deleted
  after compact display rasters are written. The rapid North America and BC
  target grids render concurrently from that one download. Fixed
  geostationary-to-map neighbour lookups are cached separately so each scan
  does not rebuild the same multi-million-point resampling tree.
- R2 publication is transactional: assets upload concurrently, the complete
  compatibility catalog commits, then `catalog-index.json` commits last.
- Full-catalog producers enqueue durable publication requests and return. A
  dedicated worker coalesces overlapping requests, while the latency-sensitive
  radar/lightning live-edge publisher remains independent.
- The publisher warns at 6.5 GB and refuses storage growth above 8 GB.
- R2 lifecycle rules expire observational frames, metadata, and video media
  after 9 days as a failure backstop.
- Local files are a bounded working set, not a second long-term archive. The
  disposable composite PNG cache is LRU-pruned to 6 GB; HLS segments, current
  videos/proxies, and the short source-frame window remain only while the
  published catalogs reference them. Health warns above a 20 GB working set,
  becomes critical above 30 GB, and separately warns below 200 GB free disk
  space (critical below 100 GB). Component totals are recorded in
  `var/status/health.json`; monitor account-wide R2 usage separately.

See [the technical assessment](docs/technical-report.md),
[production-feed setup](docs/production-feeds.md), and
[operations runbook](ops/README.md).

## Local development

Requirements: Node 22.13+, Python 3.11+, and the packages in
`requirements.txt`. Building the optional video pilot also requires ffmpeg
with libx264 (`brew install ffmpeg` on the ingest Mac).

```bash
npm install
npm run dev

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python scripts/run_ingest.py \
  --output-root public/demo --domain bc --hours 1 \
  --spool-mode auto --spool-hours 12
```

Validation:

```bash
npm run lint
npm test
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

`npm run build:pages` writes the static GitHub Pages export to `out/` with the
`/radar-sat` base path.

## Production bring-up

Use a Cloudflare Object Read & Write token restricted to the `radar-sat`
bucket. Keep its S3 key pair in macOS Keychain; never commit it.

```bash
scripts/ops/setup_local.zsh
scripts/ops/store_r2_credentials.zsh
./scripts/manage_eccc_feeds.sh check
./scripts/manage_eccc_feeds.sh install-agent

PYTHONPATH=. .venv/bin/python scripts/run_ingest.py \
  --output-root data/output --domain bc --domain north-america \
  --domain north-pacific --hours 3
PYTHONPATH=. .venv/bin/python scripts/derive_raw_visir.py \
  --output-root data/output --domain north-america
PYTHONPATH=. .venv/bin/python scripts/derive_hazard_points.py \
  --output-root data/output --dry-run
PYTHONPATH=. .venv/bin/python scripts/derive_hazard_points.py \
  --output-root data/output
PYTHONPATH=. .venv/bin/python scripts/publish_r2.py \
  --root data/output --dry-run
PYTHONPATH=. .venv/bin/python scripts/publish_r2.py --root data/output

scripts/ops/install_launchd.zsh
```

`setup_local.zsh` installs both the rendering and Sarracenia feed requirements
into the project virtual environment. After each successful render, the
scheduled cycle bounds raw staging retention to three hours by default. Native
source times are scanned over a separate 12-hour broker-recovery window before
those files can become eligible for pruning.

The scheduled cycle searches the full recent window on every run, which closes
ordinary network gaps while the three-hour GeoMet radar archive still exists.
Raw Datamart files stay local and are pruned after rendering; only compressed,
display-ready layers are sent to R2.

The optional `derive_raw_visir.py` command backfills every matching archived
`raw-visible`/`raw-ir` timestamp without re-downloading ABI/AHI source data. It
inverts the known legacy IR enhancement into an approximate monotonic neutral
temperature ramp, writes only the new `raw-visir` frame and metadata, and never
alters the source pair. Normal ingest also performs this local derivation for
the latest timestamp before deciding whether a raw NOAA download is necessary.

## Data sources

Radar-Sat uses public Environment and Climate Change Canada, NOAA GOES/AHI,
NOAA/NESDIS/STAR CIRA GeoColor, and NRCan CWFIS data. Source
limitations and fallback options are documented in the
[technical assessment](docs/technical-report.md). The interface is an
independent meteorological display and is not an official warning service.
