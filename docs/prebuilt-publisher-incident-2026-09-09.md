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

Production recovery and sustained-publication measurements are recorded after
deployment below.
