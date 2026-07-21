# Radar-Sat operations

The scheduled cycle runs every three minutes. It ingests native/GeoMet frames,
atomically rebuilds `catalog.json`, prunes raw spool files beyond the local
recovery window, publishes all referenced assets to R2, then publishes
`catalog.json` last. Raw pruning runs only after rendering; any rejected source
files are explicitly preserved for retry and surfaced by health checks. Expired
remote objects are deleted only after the catalog commit and only when their
timestamps independently violate the local retention policy. A 9-day R2
lifecycle rule is the final backstop.

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
