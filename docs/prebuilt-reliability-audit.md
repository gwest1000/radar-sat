# Prebuilt playback reliability audit — 2026-09-08

The issue is intermittent and is not specific to iPad. The user saw working loops,
then unavailable/delayed views on a work laptop, then recovery without a code
change during this audit.

## Evidence

- Initial catalog (21:42 UTC) had BC XL 3-hour weather core ending at 20:40 UTC;
  several hybrid and day views were behind the current satellite. Local health
  records independently reported 30-minute hybrid source lag against 20/25-minute
  freshness targets. Freshness rejection is therefore real, not just a bad label.
- Audit at **22:11:36 UTC** checked all **49 published composite profiles**:
  **196 HTTP checks**, covering each manifest, playlist, and first/last media
  segment, all returned 200 with the expected GitHub Pages CORS origin.
  All 49 profiles currently use segmented HLS. This is an endpoint/dependency
  sample, not a full decode test of every segment on every browser.
- In that recovered snapshot, South Coast and Southwest 3-hour and 6-hour
  operational loops ended at the latest catalog satellite slot (21:30 UTC).
- The live publisher log contains intermittent `PublicationSafetyError` failures
  for concurrently removed composite manifests and an empty lightning frame.
  These retain publication requests for a later retry, delaying new discovery.
- Local sidecar pruning protected the encoder's newest index pointer, but not
  the potentially older `catalog.json` awaiting coalesced publication.
- Manifest fetches previously made one attempt; any error blacklisted the
  generation until manual retry or a subsequent generation. An open viewer
  could also accept an older fallback catalog after an endpoint failure.
- The old playback badge used renderer eligibility, so it could claim a prebuilt
  loop before a video frame was actually presented.

## Changes

1. Protect sidecars referenced by the local catalog as well as the encoder index
   until the catalog advances. An unreadable catalog makes pruning fail closed.
2. Retry bounded discovery races for optional frame/video assets (including
   empty-file races); structural/static failures still fail immediately.
3. Preserve catalog generation monotonicity across endpoints; a network failure
   cannot replace a newer catalog with an older recovery catalog.
4. Retry transient manifest network/404/408/429/5xx failures up to three attempts,
   with a 12-second request timeout, short backoff, and cache reload on retries.
   Permission and malformed-data errors do not receive blanket retries.
5. Say "Loading prebuilt" until the selected composite has presented a frame.
   Distinguish behind-source builds from playback failures in status descriptions.

## Remaining operational limits

The scheduler prioritizes exact short loops; 6-hour exact work has a 15-minute
minimum rebuild interval, and 12/24-hour exact work has a 30-minute interval.
The September 9 investigation identified and fixed an hour-long reconciliation
blocker, stale health reports, unsafe publication handoffs, and insufficient
hybrid scheduling capacity. See
[the publisher incident report](prebuilt-publisher-incident-2026-09-09.md).
Frontend freshness limits remain intact: older imagery is not relabelled as
current to hide delays.

The public asset host is still `r2.dev`. Cloudflare documents it as a development
endpoint with variable request and bandwidth limits, and recommends a custom
domain for production. This audit did **not** observe a 429, so throttling is a
remaining infrastructure risk, not a proven cause of the user's specific failure.
See https://developers.cloudflare.com/r2/platform/limits/.

## Reproduced laptop failure: native Chromium HLS

The next user report narrowed the failure to BC SW 6/12-hour loops and supplied
the exact error: "The H.264 loop stopped making progress; using image frames."
This is a playback watchdog failure, independent of the freshness/publishing
issues above.

On the deployed site, Chrome reproduced the 6-hour failure with generation
`20260908T2240Z-8ca434219bf7`. Diagnostics showed native HLS, valid 1920x1266
metadata and an 8-second duration, but **zero presented frames**. After the
initial newest-frame seek, currentTime stayed at 7.205 with HAVE_METADATA and
only `[1.6, 2.8]` and `[6.4, 6.6]` buffered. The progress watchdog then emitted
the user's exact error. Local ffprobe checks of all 3/6/12-hour segments found
matching dimensions, durations, and continuous presentation timestamps.

The native-HLS preference introduced for iPad was too broad: Chrome now also
advertises that MIME type. Engine selection now retains hls.js for Chromium
when available, and prefers native HLS on Safari/iOS WebKit. Native-only browsers
still retain their supported player. Regression tests cover Chrome, Edge,
Chromium, Safari/iPad desktop mode, iPad, and iOS Chrome.

The corrected local preview uses the same published public assets through a
localhost proxy (production CORS does not allow the preview origin). On the
same failing 6-hour generation, hls.js buffered `[0, 8]`, reached HAVE_ENOUGH_DATA,
and presented frames through the loop. This isolates engine selection without
changing the video assets or extending the watchdog timeout.
Subsequent observations recorded 340 presented frames on the 37-frame 6-hour
loop and 126 on the 73-frame 12-hour loop. The latter buffered its full
15.2 seconds and remained in prebuilt playback after crossing the loop boundary.
The production build, ESLint, and all 19 site tests passed.
