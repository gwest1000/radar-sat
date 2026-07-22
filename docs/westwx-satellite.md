# WestWX ten-minute GOES-18 satellite path

WestWX has a dedicated North America satellite ingest that is deliberately
separate from the Forecast Graphics raw-satellite products. It reads genuine
NOAA GOES-18 ABI Level-2 full-disk scans at their nominal ten-minute cadence,
keeps the scan-start seconds from the NOAA filename, and writes only:

- `westwx-visir`: calibrated true colour in daylight blended into neutral IR
  through twilight; and
- `westwx-ir`: the existing enhanced C13 brightness-temperature rendering.

It does not change `raw-visible`, `raw-visir`, `raw-ir`, their half-hour clock,
the GOES-18/19 North America blend, or any Forecast Graphics product.
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
At roughly 250–265 MB per scan, a complete day is about 36–38 GB of source
transfer even though the retained WebP archive is much smaller. No command in
the normal pipeline starts that full backfill automatically.

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
  --max-download-gb 40 --apply
```

The second command resumes rather than redownloading the first three hours.
If current object sizes make the 40 GB boundary insufficient, it stops at a
contiguous newest-first prefix; rerun with a deliberately reviewed higher byte
cap. Status and per-scan download/render timings are written to
`data/output/status/westwx-satellite-backfill.json`.

Publication remains a separate, reviewable operation:

```bash
PYTHONPATH=. .venv/bin/python scripts/publish_r2.py \
  --root data/output --dry-run
```
