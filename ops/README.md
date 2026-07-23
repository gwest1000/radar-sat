# Radar-Sat operations

Independent three-minute full-disk and five-minute-BC satellite workers, a
five-minute observation worker, and a half-hour Pacific archive worker each use
PID locks. Each completed run atomically rebuilds `catalog.json`, publishes its
referenced assets through the shared R2 lock, then commits `catalog.json` last.
Raw pruning runs only after observation rendering; any rejected source files
are explicitly preserved for retry and surfaced by health checks. Expired remote
objects are deleted only after the catalog commit and only when their timestamps
independently violate the local retention policy. A 9-day R2 lifecycle rule is
the final backstop.

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
PYTHONPATH=. .venv/bin/python scripts/run_ingest.py --output-root data/output --domain bc --hours 168
PYTHONPATH=. .venv/bin/python scripts/publish_r2.py --root data/output --dry-run
PYTHONPATH=. .venv/bin/python scripts/publish_r2.py --root data/output
scripts/ops/install_launchd.zsh
```

The production bucket already has site CORS and a nine-day `frames/` lifecycle
backstop. Bucket configuration is a one-time control-plane operation. If the
bucket is recreated—or the optional metadata/one-day multipart rules from
`scripts/configure_r2.py` are applied—use the Cloudflare dashboard or a
separate, short-lived administrative token, then revoke it. Do not broaden the
long-lived Object Read & Write publisher token; it intentionally cannot change
bucket configuration.

The publisher warns at 4 GB and refuses growth beyond 5 GB by default. It lists
the dedicated bucket before every commit so the guard includes orphaned and
out-of-band objects, not just the local archive.

Health state is written to `var/status/health.json`; ingest and publication state
are in `data/output/status/ingest.json` and `var/status/publish.json`. Run the
checker directly with:

```bash
PYTHONPATH=. .venv/bin/python scripts/check_health.py --root data/output
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
