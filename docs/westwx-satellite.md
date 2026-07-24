# WestWX ten-minute GOES-18 satellite path

Radar-Sat has a dedicated rapid satellite ingest that is deliberately separate
from the lower-rate multi-satellite products. It reads genuine NOAA GOES-18 ABI
Level-2 full-disk scans at their nominal ten-minute cadence, keeps the scan-start
seconds from the NOAA filename, downloads each scan once, and writes:

- `westwx-visir`: calibrated true colour in daylight blended into neutral IR
  through twilight; and
- `westwx-ir`: the existing enhanced C13 brightness-temperature rendering;
- `raw-visir` and `raw-ir`: the matching BC
  renderings from the same source download.

The preferred BC display now reads CIRA GeoColor JPEGs distributed by
NOAA/NESDIS/STAR. The ten-minute full-disk product is 21,696×21,696 on the
0.5 km ABI fixed grid. South of the PACUS northern limit, the five-minute
10,000×6,000 sector is feathered over a full-disk frame. Both are reprojected
once to a 3840×2944 BC raster and retained for 24 hours. The compressed source
is deleted immediately after rendering.

This is the same GeoColor product family used by CIRA SLIDER and is materially
sharper than the 2 km multiband composite. It does not make every spectral
input physically 0.5 km: ABI C02 is 0.5 km, C01/C03 are 1 km and C13 infrared
is 2 km at nadir. CIRA's variance encoding preserves much of the high-resolution
visible texture in the colour product; nighttime detail remains constrained by
the 2 km infrared channel.

STAR distribution is a display service rather than an operationally guaranteed
feed. The calibrated NOAA Open Data full-disk/PACUS render therefore remains an
automatic fallback. The rapid compositor also constrains its northern
full-disk source time between adjacent frames, preventing a five-minute frame
from reverting to an older cloud field after a newer one has already appeared.

Radar-Sat and WestWX share the compact North America renderings. When this path
is enabled, the legacy raw ingest no longer writes duplicate half-hour BC frames;
its GOES-18/19 North America blend and Himawari-9/GOES-18 Pacific blend remain on
their lower-rate clocks.
GOES-18-only imagery cannot cover the far eastern edge as well as the blended
Forecast Graphics product; that is the intentional bandwidth tradeoff for a
ten-minute WestWX loop.

## Safe planning and backfill

The command is a dry run unless `--apply` is present. Discovery is newest-first,
already complete image/metadata pairs are skipped, and both frame count and
total compressed NOAA source bytes are hard bounds. Each source file is capped
again immediately before download. A failed scan is reported without stopping
later scans. Raw NetCDF and intermediate rasters are deleted after each scan;
only Satpy auxiliary data is cached.

Objects are discovered from NOAA's public AWS bucket, then downloaded from the
matching Google public-data mirror when available. NOAA/AWS remains an automatic
fallback if that mirror fails. This keeps discovery independent while avoiding
the materially slower AWS route observed from the production host.

The scheduled full-disk worker permits one scan and 0.8 GB of source downloads.
North America and BC render concurrently from that single scan, using persistent
nearest-neighbour lookup caches. The smaller five-minute PACUS path has its own
bounded worker so its roughly 3.5-minute processing time is not added to the
roughly 6.5-minute full-disk scan. Both use PID locks, one catalog rebuild per
run, serialized manifest-last R2 commits, and delete source scans after the
compact display rasters are installed.

## Five-minute and higher-resolution limits

NOAA's operational ABI mode provides a full disk every ten minutes. The
five-minute `MCMIPC` products are fixed regional sectors, not a second
five-minute full disk. On 2026-07-22, the embedded product bounds reached only
53.50°N for GOES-18 PACUS and 56.76°N for GOES-19 CONUS. They can improve the
southern/central part of a western display, but neither supplies reliable
five-minute coverage for all of BC or Alaska. Movable mesoscale sectors are
faster but cannot be assumed to remain over BC.

Measured multiband sector files were 55–59 MB each, or about 16–17 GB/day at
five-minute cadence per satellite. Adding both sectors would therefore add
roughly 33 GB/day of transient network transfer while still leaving northern
BC on the ten-minute full-disk clock. With the existing retention policy and
current WebP sizes, changing three BC satellite layers from ten to five minutes
for day one would add only about 0.21 GB to R2; source availability and transfer,
not retained bucket space, are the limiting factors.

The older multiband full-disk file places its true-colour composite on a 2 km
grid and is the source of the visibly blocky regional plots. Downloading the
separate native C01/C02/C03/C13 NetCDF files would total roughly 630 MB per
scan. The processed NOAA STAR route preserves the CIRA visible detail with
measured source objects of roughly 56 MB per ten-minute full disk and 29 MB per
five-minute PACUS frame. A retained 3840×2944 WebP is about 1.1–1.3 MB, so a
complete six-hour repair adds roughly 0.1 GB locally/R2 after transient source
files are deleted.

The bounded high-resolution backfill commands are:

```bash
PYTHONPATH=. .venv/bin/python scripts/backfill_noaa_star_geocolor.py \
  --sector full-disk --output-root data/output --hours 6 \
  --max-frames 36 --max-download-gb 2.5 --apply

PYTHONPATH=. .venv/bin/python scripts/backfill_noaa_star_geocolor.py \
  --sector pacus --output-root data/output --hours 6 \
  --max-frames 72 --max-download-gb 2.5 --apply
```

The normal workers are capped at one 56 MB full-disk source per ten-minute
cycle and one 29 MB PACUS source per five-minute cycle.

Inspect a one-frame benchmark plan, then download and time only that scan:

```bash
PYTHONPATH=. .venv/bin/python scripts/backfill_westwx_satellite.py \
  --output-root data/output --hours 1 --max-frames 1 \
  --max-download-gb 0.4 --benchmark

PYTHONPATH=. .venv/bin/python scripts/backfill_westwx_satellite.py \
  --output-root data/output --hours 1 --max-frames 1 \
  --max-download-gb 0.4 --benchmark --apply
```

The measured source-object sizes should be checked before widening the bounds.
At roughly 360–370 MB per scan, a complete day is about 52–54 GB of source
transfer even though the retained WebP archive is much smaller. Rendering both
grids does not require a second source download. No command in the normal
pipeline starts that full backfill automatically.

After the benchmark, the exact three-hour command is:

```bash
PYTHONPATH=. .venv/bin/python scripts/backfill_westwx_satellite.py \
  --output-root data/output --hours 3 --max-frames 18 \
  --max-download-gb 7 --apply
```

Then the exact 24-hour catch-up command is:

```bash
PYTHONPATH=. .venv/bin/python scripts/backfill_westwx_satellite.py \
  --output-root data/output --hours 24 --max-frames 144 \
  --max-download-gb 55 --apply
```

The second command resumes rather than redownloading the first three hours.
If current object sizes make the 55 GB boundary insufficient, it stops at a
contiguous newest-first prefix; rerun with a deliberately reviewed higher byte
cap. Status and per-scan download/render timings are written to
`data/output/status/westwx-satellite-backfill.json`.

Publication remains a separate, reviewable operation:

```bash
PYTHONPATH=. .venv/bin/python scripts/publish_r2.py \
  --root data/output --dry-run
```
