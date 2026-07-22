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

A second, daylight-only BC path reads the separate native C01/C02/C03/C13
files, renders one 3000×2300 composite, and retains it for 24 hours. Its source
set is deleted after each render; the standard 2 km blend remains the night and
failure fallback.

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

The scheduled production cycle permits at most two scans and 0.8 GB of source
downloads. That lets it recover the occasional scan left behind by a long
radar/Pacific cycle without starting an unbounded catch-up. Source files are
still processed one at a time and deleted after the compact display rasters
are installed.

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

The current multiband full-disk file places its true-colour composite on a 2 km
grid. Native C01/C02/C03 visible-channel files can support a roughly 1 km
composite (C02 is 0.5 km at nadir), but the measured files totalled about
603 MB per scan versus 366 MB for the present all-channel multiband source.
An all-day ten-minute high-resolution visible ingest would be about 87 GB/day
of source transfer before rendering. A daylight-only BC/regional proof is the
reasonable next experiment; merely enlarging the current raster would smooth
pixels without adding meteorological detail.

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
