# Prebuilt publication incident — September 9, 2026

## Observed failure

At 15:55 UTC the public index still had generation 14:54:25, with BC Southwest
exact 3/6-hour loops ending at 14:20 and the 12-hour loop at 14:00. The local
scheduler completed all six BC 3-hour products through 15:30 at 15:54:24.
The live browser correctly reported **Prebuilt delayed** and used newer images.
This was publication lag, independent of the September 8 Chromium HLS fix.

The publisher began a non-fast reconciliation at 14:57:28. Almost an hour later,
process sampling showed `os.link` / `shutil.copy2` local filesystem activity,
no network socket, and a partial snapshot containing 14,957 files / 1.14 GB.
The source archive is on an external APFS volume; publisher state is on the
internal volume. Every hard-link attempt crossed devices and fell back to a copy.
Reconciliation snapshotted the entire retained archive (~22,000 objects) before
checking which objects were already uploaded. Earlier reconciliations at 05:47
and 10:01 took approximately 40 and 61 minutes. Fast requests were promoted into
this slow profile, with no runtime deadline.

Health monitoring also crawled every output file, calling stat twice per file.
One check started at 15:08 and finished at 15:37; publication freshness was
evaluated against its start time, concealing the growing stall.

Production verification also exposed severe macOS Background-job throttling.
The publisher spent over three minutes discovering assets, while a read-only
foreground diagnostic validated the same ~22,000 assets in 2.23 seconds and
planned cleanup over ~30,000 uploaded records in 0.67 seconds. Sampling showed
active Python/filesystem work rather than a blocked network operation. The
publisher now uses launchd's Standard service priority. Renderers retain their
existing priorities and concurrency limits.

## Changes

- Reconciliation inventories remote objects before snapshotting. Unchanged files
  confirmed present at the expected size need no snapshot copy; missing or
  truncated objects are still repaired before catalog commit.
  New snapshots live beside the output archive on the same filesystem. Immutable
  video uses hard links; mutable imagery uses APFS copy-on-write clones so later
  corrections cannot change an upload in progress. The portable copy fallback
  remains available for unsupported filesystems and read-only source locations.
- The worker performs a fast video publication before maintenance. Each pass has
  a 300-second deadline covering its complete process group, including lock
  waiters and uploader children. Failed requests remain queued; failed
  maintenance backs off for five minutes while fresh requests can proceed.
  If fast publication itself cannot succeed, already queued reconciliation gets
  one bounded repair attempt, preserving a recovery path for capacity/index
  problems. New arrivals during that repair remain queued.
- Publisher progress identifies its current stage separately from the last
  acknowledged public catalog commit. Upload progress cannot reset catalog age.
- A durable dependency journal protects both sides of an interrupted catalog
  handoff. Replaced video dependencies remain protected for 15 minutes after
  replacement, rather than aging out based on when they were rendered. All
  pre- and post-commit cleanup respects this protection; initial rollout fails
  closed while the previous public dependency set is unknown.
- Health storage accounting uses cached directory entries and a five-second
  budget. Incomplete scans are explicitly marked, retain the date of the last
  complete sample, and still check current free disk space. Publication freshness
  is evaluated at the end of the check.
- Hybrid cores previously received one build per scheduler launch; observed
  refresh intervals reached 35 minutes against a 20-minute short-loop target.
  The scheduler now prioritizes their freshness deadlines and permits three
  builds within a 90-second process-tree budget, checking for newly due exact
  loops between builds and preserving an archive slot when exact work is clear.

Catalog commits still occur only after their dependencies upload successfully.
Frontend freshness thresholds remain unchanged.

## Validation

The regression suite exercises fresh-before-maintenance ordering, maintenance
failure/backoff, a stuck descendant that ignores TERM, retained requests and
subsequent lock acquisition, incremental reconciliation and repair, and timely
health reporting with stale acknowledged catalog commits.

Validation passed: 105 selected Python tests (47 operational publisher, 2
playback reliability, 9 snapshot/reconciliation, 12 health, 26 operational
scripts, 5 publisher worker, 4 hybrid scheduler), all 19 site tests, shell syntax,
Python compilation, plist parsing, and whitespace checks. GitHub Pages builds
and deployments for the implementation and service-priority changes succeeded.

## Production recovery

The old reconciliation eventually committed at 16:12:25, approximately 75 minutes
after starting, still using its old snapshot. The first new Background-priority
run hit the five-minute watchdog, retained its requests, and entered the bounded
repair path. This exposed the service-priority problem rather than silently
holding the publication lock indefinitely.

After the Standard-priority job reload at 16:20:47:

- Discovery and snapshotting 21,914 assets, including 1,210 pending uploads,
  reached the upload stage in **4.4 seconds**.
- The recovery catalog committed at **16:21:25**, after **38.5 seconds**.
- The subsequent authoritative reconciliation completed in **37.5 seconds**,
  snapshotting only 69 changed assets instead of the complete archive.
- Subsequent fast publications took **5.2, 5.5, 6.4, and 10.7 seconds**. The queue
  drained; new requests continued to complete after maintenance.
- An already open Southwest BC 12-hour viewer automatically returned from
  "Prebuilt delayed" to "Prebuilt loop" without reload. The 6-hour loop presented
  397 frames with its complete 8-second buffer, and the 3-hour loop presented
  480 frames with its complete 4.4-second buffer; both had zero overlay stalls.
- At **16:25:41 UTC / 09:25:41 PDT**, all **49/49** public composite profiles
  met the unchanged freshness limits. All **196/196** sampled manifest, playlist,
  first-segment and last-segment requests returned HTTP 200 with the expected
  GitHub Pages CORS origin. There were no failed requests or retries.

The first public audit had one lingering North America 24-hour weather core;
the scheduler rebuilt it at 16:24:33 and its next ordinary publication cleared
that final delayed profile. These HTTP checks sample segment availability;
browser playback checks were performed in Chrome, not on every supported device.
Separate health alerts for the upstream precipitation-type layer and aggregate
ingest status remain visible and are not relabelled as successful publication.
