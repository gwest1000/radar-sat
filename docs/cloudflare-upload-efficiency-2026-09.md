# Cloudflare publication efficiency — September 2026

## Baseline and target

Account analytics before rollout showed about 99,277 Class A requests/day. Radar-Sat accounted for 98,134; forecast graphics for 1,143. Target: fewer than 25,000/day account-wide, leaving roughly 25% headroom under the one-million monthly allowance. This is a target, not a verified post-rollout rate.

A local, lossless archive trial preserved all 10,073 observations and reduced image plus metadata objects from 20,146 to 6,640 (6,565 unique image blobs and 75 hourly metadata bundles). That is a 67% reduction in this object inventory, not a measurement of ongoing daily request savings. Eliminating duplicate publishers and timestamp-only rewrites should save additional requests.

## Publication changes

- Full and rapid publishers share `var/state/r2-publish.sqlite3`. Per-key process locks serialize duplicate PUTs; hashes compare the exact frozen bytes being uploaded. Failed requests are never recorded as successful.
- The three catalogs and live-edge index skip PUT when only their top-level generatedAt changes. A generation watermark prevents a delayed rapid publisher from rolling the index back. Full reconciliation repairs externally missing/truncated objects and invalidates their cached publication state.
- With `RADARSAT_R2_COMPACT_PUBLICATION=1`, public observation paths point to immutable `frame-blobs/<domain>/<SHA256>.<extension>`. Equal bytes share one object. Valid/source times, observation entries, and original local files remain intact. Corrections create a new URL.
- Exact original metadata is retained in compressed hourly `metadata-bundles/<domain>/<hour>.json.gz`. `catalog.json` lists these in `metadataRecovery.bundles`. Each bundle maps original metadata paths to `{metadata, blobPath}`. Recovery writes the metadata record to its original path and downloads blobPath to metadata.path; validate paths beneath the recovery directory. Bundles are gzip bytes served as application/gzip.
- Referenced blobs are protected. Unreferenced compact objects have at least two hours of remote grace; legacy image/metadata URLs have 26 hours after their last upload. Local compact files have 48 hours of grace. Reconciliation performs cleanup; no browser-visible asset is removed before the new catalog commit.
- Daily actual PUT/reuse counters live in `upload_counts`. Publisher results expose catalogUploads/contentReused. These counters exclude LISTs, retries and other software; Cloudflare analytics remain the billing authority.
- All three catalog sizes now count toward the storage guard, including the WestWX compatibility catalog.

## Frequency audit

| Product/work | Existing check cadence | Decision |
|---|---:|---|
| Lightning edge | 1 minute | Retain for new observations; skip unchanged uploads |
| MSC GeoColour edge | 1 minute | Retain arrival detection; upload only new/corrected image content |
| Radar edge | 2 minutes | Retain arrival detection; upload only changed content |
| Other satellite ingest | 3 minutes | Retain; source timestamps govern available images |
| Observations, fires, smoke | 5 minutes | Retain ingestion; sharing and hashes suppress duplicate publication |
| HRDPS/ECMWF contours | 30 minutes | Retain checks for newly available model data; no repeat PUT for unchanged bytes |
| Pacific archive/reconciliation | 30 minutes | Retain retention/repair checks and archive updates |
| Video scheduler | 1 minute check | Retain content-triggered build decisions |
| 3h loops | On changed input | Retain |
| 6h loops | 15-minute build interval | Retain |
| 12h/24h loops | 30-minute build interval | Retain |
| 7-day loops | Hourly build interval | Retain |
| HRDPS, ECMWF control/ensemble, GEFS forecast plots | 3-minute check | Retain prompt publication as plots finish; existing manifests already deduplicate |
| Static basemap, boundaries, transmission, watersheds | On change | Upload changed bytes only |

Polling an upstream source or scanning local files is not an R2 upload. Slowing polling would delay weather arrivals for little savings after content deduplication. No image quality, enhancement, frame cadence, loop duration or weather acquisition setting was changed.

## Validation and rollback

Test coverage includes concurrent full/rapid upload reuse, byte-identical file rewrites, image corrections, failed PUT retry, missing remote repair, out-of-order pointers, timestamp-only catalogs, observation-clock preservation, exact metadata recovery, dependency protection and grace periods. A real archive byte comparison also passed; cached discovery took 2.59 seconds in the trial.

Rollback compaction by setting RADARSAT_R2_COMPACT_PUBLICATION=0 and requesting a full publication. Original local image and metadata paths are retained. Old compact objects remain protected until the rollback catalog commits and their grace expires. Keep the shared ledger and content-aware uploader enabled.

Evaluate a full rolling day after rollout before claiming the 75% target or projecting a bill. Track account-wide storage as well as requests, especially the one-time overlap of legacy and compact objects during migration.

## Rollout state

Deployed September 20, 2026 (01:52–02:03 UTC September 21), after explicit user approval. Compact publication is enabled in the production .env. Normal collection, scheduler and publication intervals are unchanged.

- Ordinary retention cleanup removed approximately 540 MB before migration.
- The compact migration uploaded 6,632 assets and three catalogs, reused 27 pending assets already uploaded by the rapid path, and deleted 10,036 expired/unreferenced objects after the catalog commit. It completed in 255 seconds. Original local observations remain intact.
- The projected Radar-Sat peak was 4.726 GB, alongside approximately 4.59 GB in forecast graphics. Account-wide peak remained an estimate rather than an invoice measurement.
- Public verification found 10,086 compact observation records and 6,584 unique image paths. All 55 manifests available immediately before migration remained available. All 105 sampled images passed checksum verification. BC XL prebuilt and dynamic satellite/radar/lightning playback were visually checked in Chrome with no console errors.
- A subsequent ordinary publication completed in 5.4 seconds, uploading six changed assets and two catalogs.
- All 22 targeted publication tests and 11 billing-notification tests passed. The wider operations suite retains three previously identified failures involving retired hybrid presets; none was introduced by this rollout.
- Shared cleanup locking protects a rapid publisher reintroducing an older image. Cleanup releases the lock after each batch of at most 1,000 deletions, allowing live observations to proceed between batches.

The final remote inventory checked all 6,655 referenced image/static/bundle assets: none was missing. Radar-Sat held 4.257 GB of object payload, making transitional account storage approximately 8.9 GB when combined with forecast graphics and metadata. Older migration duplicates will be removed by the configured grace/retention rules.

Daily request savings are not yet verified. The one-time migration must be excluded from operating-rate estimates. The existing account-wide billing monitor remains active; notification reporting now explicitly distinguishes a submitted Mac notification or Telegram API acceptance from confirmed delivery. No new phone/email destination has been configured.

Local verification records are in `/Users/greg/projects/fcstGraphics/experiments/cloudflare-upload-efficiency/`.
