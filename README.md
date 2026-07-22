# BC Satellite/Radar/Lightning

Operational satellite, radar, precipitation-type, and lightning loops for
British Columbia and its upstream weather. The viewer uses one set of aligned
source layers to build several products in the browser, so a new overlay does
not duplicate imagery in R2.

Public site: <https://gwest1000.github.io/radar-sat/>

## Launch products

- **Overlay:** BC Large, tightly cropped BC Small, southwest, southeast and
  northeast views. Each provides mutually exclusive visible, IR, day/night
  VisIR and convective-sandwich satellite backgrounds; mutually exclusive
  radar/precipitation-type overlays; and independent lightning and wildfire
  hotspots.
- **Snow / fog:** BC Small, southwest, southeast and northeast qualitative RGB
  loops.
- **North America:** configurable raw GOES-18/19 true colour or enhanced IR,
  with the ECCC continental radar composite and honest coverage hatching.
- **North Pacific:** Himawari-9/GOES-18 true colour or enhanced IR on a
  dateline-safe grid, with the real West Coast radar footprint.

The regional displays crop the shared aligned BC grid, gaining on-screen detail
without storing four duplicate seven-day archives. The watershed overlay uses
the same 54-polygon BC Hydro boundary source as the forecast-model plots.

Every map shows the real source timestamps. Old data is never silently relabelled
as current, and hatched grey means no current radar coverage rather than no echo.
Lightning density cells are rendered as white-ringed flash markers that fade
with age rather than opaque grid squares.
Wildfire hotspots are archived ten-minute snapshots of NRCan CWFIS satellite
thermal detections, shown as age-coloured diamonds; they are not fire
perimeters or confirmation of an active wildfire.

## Architecture

```text
ECCC Datamart AMQPS ── GOES / lightning / site radar ┐
ECCC GeoMet WMS ────── composite / ptype / coverage ├─ local render + retention
NOAA public S3 ──────── GOES/AHI + GLM/ADP hazards ─┤
                                                     └─ R2 layers + catalog.json
                                                                  │
GitHub Pages static viewer ◀──────────────────────────────────────┘
```

- BC grid: EPSG:3005, 1920×1472, approximately 145–108°W and 45–63°N.
- BC retention: all observations for 24 hours, then `:00`/`:30` through day 7
  (432 ten-minute or 528 six-minute times per layer).
- Broad satellite target: 30 minutes for 24 hours, then hourly through day 7;
  GOES-18 smoke and total lightning are processed on a separate 10-minute clock.
- `raw-visir` is a server-rendered true-colour day / neutral 10.3–10.4 µm IR
  night image. A solar-elevation smoothstep removes the false-colour terminator
  fringe, low-sun chroma is faded separately, and a bounded overlap correction
  softens the GOES-18/19 colour seam.
- Geostationary scan-edge pixels missing from both visible and infrared are
  transparent; the renderer does not synthesize weather into those gaps.
- Raw NOAA source files are handled sequentially under a 900 MB hard cache
  cap for multiband imagery or a 100 MB per-object hazard cap, then deleted
  after compact display rasters are written.
- R2 publication is transactional: assets first, `catalog.json` last.
- The publisher warns at 4 GB and refuses storage growth above 5 GB.
- The R2 `frames/` lifecycle expires at 9 days as a failure backstop.
- Allow roughly 8–10 GB free on the ingest host for processed output plus the
  local native-data spool; monitor account-wide R2 usage separately.

See [the technical assessment](docs/technical-report.md),
[production-feed setup](docs/production-feeds.md), and
[operations runbook](ops/README.md).

## Local development

Requirements: Node 22.13+, Python 3.11+, and the packages in
`requirements.txt`.

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
and NRCan CWFIS data. Source
limitations and fallback options are documented in the
[technical assessment](docs/technical-report.md). The interface is an
independent meteorological display and is not an official warning service.
