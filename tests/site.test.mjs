import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

import { shouldWaitForSequentialSurface } from "../app/video-playback-guard.ts";
import { appendLiveEdgeFrame } from "../app/live-edge-timeline.ts";

test("adds one combined live-edge frame after a regular timeline", () => {
  const regular = [
    { validTime: "2026-09-04T00:20:00Z", path: "20.png" },
    { validTime: "2026-09-04T00:30:00Z", path: "30.png" },
  ];
  const result = appendLiveEdgeFrame(regular, [
    "2026-09-04T00:36:00Z",
    "2026-09-04T00:40:00Z",
    "not-a-date",
  ]);
  assert.deepEqual(result.map((frame) => frame.validTime), [
    "2026-09-04T00:20:00Z",
    "2026-09-04T00:30:00Z",
    "2026-09-04T00:40:00.000Z",
  ]);
  assert.equal(result[2].path, "30.png");
  assert.equal(appendLiveEdgeFrame(regular, ["2026-09-04T00:30:00Z"]).length, 2);
});

test("holds the committed weather frame until its sequential overlay is ready", () => {
  const state = {
    playing: true,
    fullyComposited: false,
    nativeLoop: false,
    currentIndex: 4,
    frameCount: 10,
    nextSurfaceReady: false,
  };
  assert.equal(shouldWaitForSequentialSurface(state), true);
  assert.equal(shouldWaitForSequentialSurface({ ...state, nextSurfaceReady: true }), false);
  assert.equal(shouldWaitForSequentialSurface({ ...state, fullyComposited: true }), false);
  assert.equal(shouldWaitForSequentialSurface({ ...state, playing: false }), false);
  assert.equal(
    shouldWaitForSequentialSurface({ ...state, currentIndex: 9, nativeLoop: false }),
    false,
  );
  assert.equal(
    shouldWaitForSequentialSurface({ ...state, currentIndex: 9, nativeLoop: true }),
    true,
  );
});

test("exports the operational viewer", async () => {
  const html = await readFile(new URL("../out/index.html", import.meta.url), "utf8");
  assert.match(html, /Real-Time Wx Display/);
  assert.match(html, /href="\/radar-sat\/_next\//);
  assert.match(html, /href="\/radar-sat\/favicon\.svg"/);
  assert.match(html, /https:\/\/gwest1000\.github\.io\/radar-sat\/og-radar-sat\.png/);
  assert.doesNotMatch(html, /radar-sat\/radar-sat\/og-radar-sat\.png/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape/);
});

test("refreshes the runtime catalog for long-open displays", async () => {
  const viewer = await readFile(new URL("../app/radar-viewer.tsx", import.meta.url), "utf8");
  assert.match(viewer, /setInterval\(load, 60_000\)/);
  assert.match(viewer, /clearInterval\(interval\)/);
  assert.match(viewer, /searchParams\.set\("v", frame\.fetchedAt\)/);
  assert.match(viewer, /filter\(\(item\) => productHasFrames\(catalog, item\)\)/);
  assert.match(viewer, /actualSourceTime\(item\.id, item\.frame\)/);
  assert.match(viewer, /RANGE_OPTIONS = \[3, 6, 12, 24, 168\]/);
  assert.match(viewer, /REGION_MENU_BOTTOM_TO_TOP = \[\s*"bc-south-coast-overlay",\s*"bc-southwest-overlay",\s*"bc-southeast-overlay",\s*"bc-northeast-overlay",\s*"bc-small-overlay",\s*"bc-large-overlay",\s*"pacific-wna-overlay",\s*"north-america-overlay",\s*"north-pacific-overlay",\s*\]/);
  assert.match(viewer, /REGION_MENU_TOP_TO_BOTTOM = \[\.\.\.REGION_MENU_BOTTOM_TO_TOP\]\.reverse\(\)/);
  assert.match(viewer, /regionMenuProducts\.map\(\(item\) =>/);
  assert.match(viewer, /rangeMenuOptions\.map\(\(hours\) =>/);
  assert.match(viewer, /function playbackFrames/);
  assert.match(viewer, /SAME_SLOT_TOLERANCE_MS/);
  assert.match(viewer, /const frame = selectedFrame \?\?/);
  assert.match(viewer, /product\.frameIntervalMinutes/);
  assert.match(viewer, /product\.dayFrameIntervalMinutes/);
  assert.match(viewer, /product\.archiveFrameIntervalMinutes/);
  assert.match(viewer, /const regularFrames = playbackFrames\([\s\S]*?product\.frameIntervalMinutes,[\s\S]*?product\.dayFrameIntervalMinutes,[\s\S]*?product\.archiveFrameIntervalMinutes/);
  assert.match(viewer, /return appendLiveEdgeFrame\(regularFrames, candidateTimes\)/);
  assert.doesNotMatch(viewer, /Promise\.all\(loads\)/);
  assert.doesNotMatch(viewer, /lightningFlashLayerId|flashDisplayAge/);
  assert.match(viewer, /atOrBeforeSourceTime/);
  assert.match(viewer, /sourceCount > selectedSourceCount/);
  assert.match(viewer, /PLAYBACK_SPEEDS = \[0\.25, 0\.5, 0\.75, 1, 1\.5, 2, 3, 4\]/);
  assert.match(viewer, /useState\(3\)/);
  assert.match(viewer, /\? stored\.speedIndex\s*: 3/);
  assert.match(viewer, /110 \* stepFactor/);
  assert.match(viewer, /\+ \(finalFrame \? 215 : 0\)/);
  assert.match(viewer, /pageVisible && anchorFrames\.length > 1/);
  assert.match(viewer, /setPageVisible\(document\.visibilityState === "visible"\)/);
  assert.doesNotMatch(viewer, /document\.hasFocus\(\)/);
  assert.doesNotMatch(viewer, /window\.addEventListener\("blur"/);
  assert.match(viewer, /IMAGE_FRAME_CACHE_LIMIT = 16/);
  assert.match(viewer, /image: HTMLImageElement/);
  assert.match(viewer, /releasePreloadedImage\(loadedSrc\)/);
  assert.match(viewer, /function StableMapImage/);
  assert.match(viewer, /imageFrameCache\.delete\(url\)/);
  assert.match(viewer, /advanceWhenReady/);
  assert.match(viewer, /criticalUrls/);
  assert.match(viewer, /PLAYBACK_IMAGE_RETRIES/);
  assert.match(viewer, /setActiveSlot\(slotIndex\)/);
  assert.match(viewer, /data-buffer-state=/);
  assert.match(viewer, /requestedSrcRef\.current !== loadedSrc/);
  assert.match(viewer, /atOrBefore\(nativeLayer\?\.frames \?\? \[\], anchor\.validTime/);
  assert.match(viewer, /setPlaying\(true\)/);
  assert.match(viewer, /activeAnchorLayer/);
  assert.match(viewer, /enabledChoices\.length === 1/);
  assert.match(viewer, /enabledChoices\.length > 1/);
  assert.match(viewer, /Prefer an explicit true/);
  assert.match(viewer, /> Radar coverage</);
  assert.doesNotMatch(viewer, /> No radar coverage</);
  assert.match(viewer, /method: "HEAD"/);
  assert.match(viewer, /previousGeneration === nextCatalog\.generatedAt/);
  assert.doesNotMatch(viewer, /window\.location\.reload\(\)/);
  assert.match(viewer, /VIEWER_PREFERENCES_KEY/);
  assert.match(viewer, /function mscPrimaryFrames/);
  assert.match(viewer, /now - slot \* intervalMs >= 35 \* 60_000/);
});

test("uses an atomic H.264 compositor for complete live and archive profiles", async () => {
  const viewer = await readFile(new URL("../app/radar-viewer.tsx", import.meta.url), "utf8");
  const videoLoop = await readFile(new URL("../app/video-loop.ts", import.meta.url), "utf8");
  const compositor = await readFile(new URL("../app/video-composite-stage.tsx", import.meta.url), "utf8");
  const styles = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.doesNotMatch(viewer, /VIDEO_PILOT_LAYERS/);
  assert.match(viewer, /effectiveRangeHours === 24[\s\S]*?\? "day"[\s\S]*?: effectiveRangeHours > 24[\s\S]*?\? "archive"[\s\S]*?: "live"/);
  assert.match(viewer, /videoProfiles\?\.\[product\.id\]\?\.\[videoLayerId\]\?\.\[videoTrack\]/);
  assert.match(viewer, /videoPlans\.length === videoAnchorFrames\.length/);
  assert.match(viewer, /proxy\.width !== playbackVideoManifest\.width/);
  assert.match(viewer, /if \(videoModeReady \|\| !isAnimating/);
  assert.match(viewer, /data-renderer=\{videoModeReady \? "video" : "images"\}/);
  assert.match(viewer, /<VideoCompositeStage/);
  assert.match(viewer, /VIDEO_HUD_UPDATE_INTERVAL_MS = 180/);
  assert.match(viewer, /presentedVideoIndexRef/);
  assert.match(viewer, /timelineRangeRef\.current\.value = String\(index\)/);
  assert.match(viewer, /timelineRangeRef\.current\.value = String\(index\)[\s\S]*?now - lastVideoHudUpdateAtRef\.current < VIDEO_HUD_UPDATE_INTERVAL_MS/);
  assert.match(viewer, /timelineRangeRef\.current\.value = String\(currentFrameIndex\)/);
  assert.match(viewer, /defaultValue=\{currentFrameIndex\}/);
  assert.doesNotMatch(viewer, /value=\{currentFrameIndex\}/);
  assert.match(viewer, /PlaybackStatusLines/);
  assert.match(viewer, /playbackStatusLinesRef\.current\?\.update\(displayedFrame\.validTime, times\)/);
  assert.match(viewer, /useEffect\(\(\) => \{\s*update\(initialValidTime, initialSourceTimes\)/);
  assert.doesNotMatch(viewer, /validLineRef/);
  assert.doesNotMatch(viewer, /setFrameIndex\(\(current\) => current === index \? current : index\)/);
  assert.match(videoLoop, /transport: "progressive-mp4"/);
  assert.match(videoLoop, /track: "live" \| "day" \| "archive"/);
  assert.match(videoLoop, /mediaViewport/);
  assert.match(videoLoop, /proxies: Record<string, VideoProxy>/);
  assert.match(videoLoop, /proxyLayers: VideoProxyLayerSelection\[\]/);
  assert.match(videoLoop, /MAX_MANIFEST_CACHE_ENTRIES = 8/);
  assert.match(videoLoop, /manifestCache\.size > MAX_MANIFEST_CACHE_ENTRIES/);
  assert.match(videoLoop, /requestVideoFrameCallback/);
  assert.match(viewer, /selectedProxyLayers\(frame\.proxyLayers, enabledIds\)/);
  assert.match(viewer, /playbackVideoManifest\.proxies\[selection\.sourceKey\]/);
  assert.match(viewer, /activeVideoProxyLayers/);
  assert.match(viewer, /const pointSourceTimes = \(videoModeReady \? \[\] : \[/);
  assert.match(viewer, /if \(!videoModeReady && lightningController/);
  assert.match(viewer, /if \(!videoModeReady && fireController/);
  assert.match(compositor, /class SurfaceCache/);
  assert.match(compositor, /displayViewport\.left - mediaViewport\.left/);
  assert.match(compositor, /manifest\.media\.contentHeight \?\? manifest\.media\.height/);
  assert.match(compositor, /host\.append\(next\)/);
  assert.match(compositor, /visibleSurfacesRef/);
  assert.doesNotMatch(compositor, /underlayHost\.replaceChildren/);
  assert.doesNotMatch(compositor, /overlayHost\.replaceChildren/);
  assert.match(compositor, /resizeWidth: this\.width/);
  assert.doesNotMatch(compositor, /context\.drawImage\(\s*video/);
  assert.match(compositor, /MAX_CONCURRENT_OVERLAY_DECODES = 3/);
  assert.match(compositor, /Overlay decoding was cancelled/);
  assert.match(compositor, /bitmapCache\.activate\(\)/);
  assert.match(compositor, /index === committedIndexRef\.current/);
  assert.match(compositor, /HIGH_MEMORY_SURFACE_CACHE_BYTES = 384/);
  assert.match(compositor, /surfaceCacheEntryLimit/);
  assert.match(compositor, /surfaceCache\.setLimit\(surfaceEntryLimit\)/);
  assert.match(compositor, /surfaceCache\.retain\(planCacheKeys\)/);
  assert.match(compositor, /const readyToResume = committedIndexRef\.current < 0/);
  assert.match(compositor, /const delay = plans\[index\]\.frame\.durationSeconds \* 1_000 \/ speedRef\.current/);
  assert.match(compositor, /data-surface-cache-limit/);
  assert.match(compositor, /data-surface-builds/);
  assert.match(compositor, /if \(playingRef\.current\) return/);
  assert.match(compositor, /plan\.frame\.ptsSeconds[\s\S]*Math\.min\(0\.005, plan\.frame\.durationSeconds \/ 4\)/);
  assert.match(compositor, /video\.readyState < HTMLMediaElement\.HAVE_CURRENT_DATA/);
  assert.match(compositor, /if \(!playingRef\.current\) video\.pause\(\)/);
  assert.match(compositor, /if \(speed >= 4\) return 12/);
  assert.match(compositor, /PLAYBACK_SURFACE_PIXELS = 1_300_000/);
  assert.match(compositor, /PREWARM_BITMAP_CACHE_BYTES = 32/);
  assert.match(compositor, /ACTIVE_BITMAP_CACHE_BYTES = BITMAP_CACHE_BYTES - PREWARM_BITMAP_CACHE_BYTES/);
  assert.match(compositor, /shouldWaitForSequentialSurface/);
  assert.match(compositor, /video\.pause\(\);[\s\S]*surfaceCache\.prepare\(plan\)\.then\(beginSeek\)/);
  assert.match(compositor, /playbackSurfaceSize\(manifest\.width, manifest\.height\)/);
  assert.match(compositor, /crossOrigin="anonymous"/);
  assert.match(compositor, /requestVideoFrameCallback/);
  assert.match(compositor, /data-overlay-stalls="0"/);
  assert.match(videoLoop, /defaultComposite\?: VideoDefaultComposite/);
  assert.match(videoLoop, /composites\?: VideoCompositePreset\[\]/);
  assert.match(videoLoop, /export function exactCompositeManifest/);
  assert.match(videoLoop, /candidate\.hours === rangeHours/);
  assert.match(videoLoop, /const durationSeconds = range\.durationsSeconds\[index\]/);
  assert.match(videoLoop, /validDefaultComposite/);
  assert.match(videoLoop, /const defaultComposite = parsed\.defaultComposite/);
  assert.match(videoLoop, /const composites = Array\.isArray\(parsed\.composites\)/);
  assert.match(viewer, /sameLayerSet\(enabledVideoLayerIds, candidateDefaultComposite\.layerIds\)/);
  assert.match(viewer, /mediaViewport: activeExactComposite\.manifest\.mediaViewport \?\? FULL_VIEWPORT/);
  assert.match(viewer, /satelliteFilter=\{activeComposite \? undefined : satelliteFilter\}/);
  assert.match(viewer, /nativeLoop=\{activeComposite\?\.nativeLoop\}/);
  assert.match(viewer, /playbackQuality/);
  assert.match(viewer, /label: "Prebuilt loop"/);
  assert.match(viewer, /"Loading prebuilt"/);
  assert.match(viewer, /label: "Dynamic layers"/);
  assert.match(viewer, /data-playback-build={playbackBuildStatus\.mode}/);
  assert.match(viewer, /className={`decoder-selector\$\{decoderMenuOpen \? " is-open" : ""\}`}/);
  assert.match(viewer, /className="layers-summary decoder-summary"/);
  assert.match(viewer, /className="layers-popover decoder-popover"/);
  assert.match(viewer, /role="group" aria-label="Playback decoder"/);
  assert.match(viewer, /type PlaybackQuality = "high"/);
  assert.doesNotMatch(viewer, /name="playback-quality"/);
  assert.match(styles, /\.layer-toolbar\s*\{/);
  assert.match(styles, /\.decoder-selector:hover \.decoder-popover/);
  assert.match(styles, /\.decoder-options\s*\{/);
  assert.match(styles, /\.playback-build-indicator\.is-prebuilt/);
  assert.match(styles, /\.playback-build-indicator\.is-hybrid/);
  assert.match(styles, /\.playback-build-indicator\.is-dynamic/);
  assert.doesNotMatch(viewer, /navigator\.mediaCapabilities/);
  assert.match(viewer, /rememberCompositeMediaFailure/);
  assert.match(viewer, /data-composite-preset=/);
  assert.match(compositor, /const fullyComposited = Boolean\(compositePresetId\)/);
  assert.match(compositor, /fullyComposited[\s\S]*?Promise\.resolve\(\[EMPTY_PREPARED_SURFACES\]\)/);
  assert.match(compositor, /commitSurfaces\(prepared\)/);
  assert.match(compositor, /getVideoPlaybackQuality/);
  assert.match(compositor, /loop=\{nativeLoop\}/);
  assert.match(compositor, /data-cadence-p95-error-ms/);
  assert.match(compositor, /data-boundary-gap-ms/);
  assert.match(compositor, /weatherFramesSkipped/);
  assert.match(compositor, /weatherFramesOutOfOrder/);
  assert.match(compositor, /data-frame-processing-ms/);
  assert.match(compositor, /VIDEO_PROGRESS_TIMEOUT_MS = 30_000/);
  assert.match(compositor, /maxBufferSize: HLS_BUFFER_BYTES/);
  assert.match(compositor, /HLS_LIVE_BUFFER_SECONDS = 40/);
  assert.match(compositor, /HLS_ARCHIVE_BUFFER_SECONDS = 45/);
  assert.match(compositor, /HLS_ARCHIVE_MAX_BUFFER_SECONDS = 60/);
  assert.match(compositor, /frontBufferFlushThreshold: liveTrack/);
  assert.doesNotMatch(compositor, /maxMaxBufferLength: 300/);
  assert.match(compositor, /stopped making progress; using image frames/);
  assert.match(compositor, /video\.addEventListener\("ended", onEnded\)/);
  assert.match(compositor, /operationEpochRef/);
  assert.match(compositor, /invalidateOperation/);
  assert.match(compositor, /previousPlanRevisionRef/);
  assert.match(compositor, /removeEventListener\("seeked"/);
  assert.match(compositor, /disposedRef\.current \|\| failedRef\.current/);
  assert.match(compositor, /disposedRef\.current = true;[\s\S]*?invalidateOperation\(video\)/);
  assert.match(styles, /\.video-composite-canvas/);
  assert.match(styles, /\.video-loop-decoder/);
  assert.doesNotMatch(styles, /data-presentation-ready="false"/);
});

test("prefers exact-range composite sidecars and fails open to legacy playback", async () => {
  const viewer = await readFile(new URL("../app/radar-viewer.tsx", import.meta.url), "utf8");
  const videoLoop = await readFile(new URL("../app/video-loop.ts", import.meta.url), "utf8");
  const compositor = await readFile(new URL("../app/video-composite-stage.tsx", import.meta.url), "utf8");

  assert.match(videoLoop, /export type CompositeProfilePointer/);
  assert.match(videoLoop, /export type CompositeLoopManifest/);
  assert.match(videoLoop, /export function parseCompositeLoopManifest/);
  assert.match(videoLoop, /export function matchingCompositeProfile/);
  assert.match(videoLoop, /export function matchingHybridCompositeProfile/);
  assert.match(videoLoop, /compositeKind\?: "exact" \| "hybrid-prefix"/);
  assert.match(videoLoop, /bakedLayerIds\?: string\[\]/);
  assert.match(videoLoop, /eligibleOverlayLayerIds\?: string\[\]/);
  assert.match(videoLoop, /proxyLayers\?: VideoProxyLayerSelection\[\]/);
  assert.match(videoLoop, /pointer\.rangeHours === rangeHours/);
  assert.match(videoLoop, /pointer\.layerIds\.every\(\(id\) => selected\.has\(id\)\)/);
  assert.match(videoLoop, /manifest\.endValidTime[\s\S]*finalFrame\.validTime/);
  assert.match(videoLoop, /manifest\.endSourceTime[\s\S]*finalFrame\.sourceValidTime/);
  assert.match(videoLoop, /export function compositeLoopVideoManifest/);
  assert.match(videoLoop, /proxies: manifest\.proxies \?\? \{\}/);

  assert.match(viewer, /compositeProfiles\?: Record/);
  assert.match(viewer, /matchingCompositeProfile\([\s\S]*enabledVideoLayerIds,[\s\S]*effectiveRangeHours/);
  assert.match(viewer, /COMPOSITE_FRESHNESS_MINUTES[\s\S]*3: 20[\s\S]*6: 25[\s\S]*12: 40[\s\S]*24: 40[\s\S]*168: 70/);
  assert.match(viewer, /const exactComposite = candidateCompositeManifest\?\.compositeKind === "hybrid-prefix"[\s\S]*usableLegacyExactComposite \?\? usableSidecarComposite/);
  assert.match(viewer, /const needsFullCatalog = compositeUnavailable && legacyUnavailable/);
  assert.match(viewer, /pendingCompositeManifestRef\.current[\s\S]*handleVideoLoopBoundary/);
  assert.match(viewer, /acceptedCompositeGeneration === loadedCompositeManifest\.generation/);
  assert.match(viewer, /retiredStaleComposite[\s\S]*setLoadedCompositeManifest\(null\)/);
  assert.match(viewer, /pendingMediaReadyKeyRef\.current !== readyKey/);
  assert.match(viewer, /pendingOverlayReadyKeyRef\.current !== readyKey/);
  assert.match(viewer, /label: "Prebuilt core \+ layers"/);
  assert.match(viewer, /selectedProxyLayers/);
  assert.match(viewer, /selection\.ids \?\? \[selection\.id\]/);
  assert.match(viewer, /sort\(\(left, right\) => left\.configured\.index - right\.configured\.index\)/);
  assert.match(viewer, /prewarmVideoCompositeSurfaces/);
  assert.match(compositor, /takePrewarmedSurface/);
  assert.match(compositor, /PREWARM_SURFACE_CACHE_BYTES = 8/);
  assert.match(
    viewer,
    /const pendingSelectionKey = pendingSelectionCandidate[\s\S]*exactCompositeSelectionKey\("legacy", pendingSelectionCandidate\)/,
  );
  assert.match(viewer, /onCanPlay=\{\(event\) => \{[\s\S]*HTMLMediaElement\.HAVE_FUTURE_DATA/);
  assert.match(viewer, /key=\{pendingExactCompositeKey\}/);
  assert.match(compositor, /index < plans\.length - 1 \|\| nativeLoop/);
});

test("falls through stale or failed exact profiles without resurrecting failed media", async () => {
  const {
    canRetainLoadedComposite,
    pendingMediaFailureTransition,
    preferredCompositeProfile,
    rememberCompositeMediaFailure,
    rememberFailedKey,
    requiresPendingMediaPreload,
    shouldQueueCompositeHandoff,
  } = await import("../app/video-selection-policy.ts");
  const pointer = (presetId, compositeKind) => ({
    compositeKind,
    presetId,
    layerIds: ["base-dark", "satellite"],
    rangeHours: 3,
    generation: "generation-a",
    manifestPath: `video/${presetId}.json`,
    generatedAt: "2026-08-21T12:10:00Z",
    endValidTime: "2026-08-21T12:10:00Z",
    endSourceTime: "2026-08-21T12:10:00Z",
  });
  const exact = {
    pointer: pointer("exact-v1", "exact"),
    failureKey: "exact-key",
    fresh: true,
  };
  const hybrid = {
    pointer: pointer("weather-smoke-core-v1", "hybrid-prefix"),
    failureKey: "hybrid-key",
    fresh: true,
  };

  assert.equal(preferredCompositeProfile(exact, hybrid, [])?.pointer.presetId, "exact-v1");
  assert.equal(
    preferredCompositeProfile({ ...exact, fresh: false }, hybrid, [])?.pointer.presetId,
    "weather-smoke-core-v1",
  );
  assert.equal(
    preferredCompositeProfile(exact, hybrid, ["exact-key"])?.pointer.presetId,
    "weather-smoke-core-v1",
  );
  assert.equal(preferredCompositeProfile(exact, hybrid, ["exact-key", "hybrid-key"]), null);

  let failedMedia = rememberCompositeMediaFailure({}, "sidecar-key", "sidecar failed");
  failedMedia = rememberCompositeMediaFailure(failedMedia, "legacy-key", "legacy failed");
  assert.deepEqual(failedMedia, {
    "sidecar-key": "sidecar failed",
    "legacy-key": "legacy failed",
  });
  assert.deepEqual(
    rememberFailedKey(rememberFailedKey([], "exact-key"), "hybrid-key"),
    ["exact-key", "hybrid-key"],
  );
  assert.equal(requiresPendingMediaPreload({ manifest: { transport: "hls-ts" } }), false);
  assert.equal(requiresPendingMediaPreload({ manifest: { transport: "progressive-mp4" } }), true);

  const activeHybrid = {
    generation: "hybrid-generation-a",
    productId: "test-product",
    layerId: "satellite",
    track: "live",
    presetId: "weather-smoke-core-v1",
    rangeHours: 3,
  };
  const incomingExact = {
    ...activeHybrid,
    generation: "exact-generation-b",
    presetId: "exact-v1",
  };
  assert.equal(
    canRetainLoadedComposite(activeHybrid, incomingExact, activeHybrid.generation),
    true,
    "an accepted hybrid remains visible while an exact target is loading",
  );
  assert.equal(
    canRetainLoadedComposite(activeHybrid, null, activeHybrid.generation),
    true,
    "an accepted stale profile remains visible until its loop boundary",
  );
  assert.equal(canRetainLoadedComposite(activeHybrid, incomingExact, ""), false);
  assert.equal(
    shouldQueueCompositeHandoff(
      activeHybrid,
      incomingExact,
      activeHybrid.generation,
      true,
    ),
    true,
    "a cross-preset replacement queues behind a proven active circuit",
  );
  assert.equal(
    shouldQueueCompositeHandoff(
      activeHybrid,
      incomingExact,
      activeHybrid.generation,
      false,
    ),
    false,
    "an incompatible old circuit cannot strand a replacement in pending state",
  );

  assert.deepEqual(
    pendingMediaFailureTransition("sidecar", "sidecar-key", "sidecar-profile"),
    {
      discardPendingComposite: true,
      discardPendingVideo: false,
      failedMediaKey: "sidecar-key",
      failedProfileKey: "sidecar-profile",
    },
    "a rejected sidecar is removed so it cannot block the legacy pending source",
  );
  assert.deepEqual(
    pendingMediaFailureTransition("legacy", "legacy-key", "legacy-profile"),
    {
      discardPendingComposite: false,
      discardPendingVideo: false,
      failedMediaKey: "legacy-key",
      failedProfileKey: "",
    },
    "a rejected legacy rendition preserves its base pending video manifest",
  );
});

test("sanitizes legacy and range composites independently", async () => {
  const {
    exactCompositeManifest,
    parseVideoLoopManifest,
  } = await import("../app/video-loop.ts");
  const media = {
    path: "videos/exact.mp4",
    mimeType: "video/mp4",
    codec: "avc1",
    width: 10,
    height: 10,
    byteLength: 1,
    sha256: "abc123",
  };
  const frame = (index, validTime) => ({
    index,
    validTime,
    sourceValidTime: validTime,
    encodedSourceLayer: "satellite",
    sourcePath: `satellite-${index}.webp`,
    sourceFetchedAt: validTime,
    ptsSeconds: index,
    durationSeconds: 1,
    proxyLayers: [],
  });
  const parsed = parseVideoLoopManifest({
    schemaVersion: 2,
    generation: "generation-a",
    generatedAt: "2026-08-21T12:10:00Z",
    productId: "test-product",
    layerId: "satellite",
    track: "live",
    transport: "progressive-mp4",
    cadenceMinutes: 10,
    width: 10,
    height: 10,
    media,
    frames: [
      frame(0, "2026-08-21T12:00:00Z"),
      frame(1, "2026-08-21T12:10:00Z"),
    ],
    proxies: {},
    defaultComposite: {},
    composites: [
      {
        id: "malformed-matching-preset",
        layerIds: ["base-dark", "satellite"],
        mediaViewport: { left: 0, top: 0, width: 1, height: 1 },
      },
      {
        id: "healthy-matching-preset",
        layerIds: ["base-dark", "satellite"],
        mediaViewport: { left: 0, top: 0, width: 1, height: 1 },
        ranges: [{
          hours: 3,
          firstFrame: 0,
          frameCount: 2,
          durationsSeconds: [1, 4],
          boundaryIntervalMultiplier: 4,
          renditions: [{ id: "high", media }],
        }],
      },
    ],
  });

  assert.equal(parsed.defaultComposite, undefined);
  assert.deepEqual(parsed.composites?.map((preset) => preset.id), ["healthy-matching-preset"]);
  assert.doesNotThrow(() => exactCompositeManifest(
    parsed,
    ["base-dark", "satellite"],
    3,
    "high",
  ));
  assert.equal(
    exactCompositeManifest(parsed, ["base-dark", "satellite"], 3, "high")?.presetId,
    "healthy-matching-preset",
  );
});

test("preserves immutable baked source times for hybrid freshness", async () => {
  const {
    compositeLoopVideoManifest,
    parseCompositeLoopManifest,
    videoFrameSourceTimeMap,
  } = await import("../app/video-loop.ts");
  const proxy = {
    path: "video-proxies/model-contours.webp",
    width: 10,
    height: 10,
    byteLength: 1,
    sha256: "def456",
  };
  const media = {
    path: "videos/hybrid.mp4",
    mimeType: "video/mp4",
    codec: "avc1",
    width: 10,
    height: 11,
    contentHeight: 10,
    byteLength: 1,
    sha256: "abc123",
  };
  const hybridFrame = (validTime, radarTime, modelTime) => ({
    validTime,
    sourceValidTime: validTime,
    layerSourceTimes: {
      "base-dark": null,
      satellite: validTime,
      smoke: validTime,
      "radar-rain": radarTime,
    },
    proxyLayers: [{
      id: "model-contours",
      ids: ["model-mslp", "model-hgt500"],
      renderId: "model-contours",
      sourceKey: proxy.path,
      sourceValidTime: modelTime,
      sourceValidTimes: {
        "model-mslp": modelTime,
        "model-hgt500": modelTime,
      },
    }],
    durationSeconds: 1,
  });
  const parsed = parseCompositeLoopManifest({
    schemaVersion: 2,
    compositeKind: "hybrid-prefix",
    generation: "generation-b",
    generatedAt: "2026-08-21T12:10:00Z",
    productId: "test-product",
    domainId: "test-domain",
    layerId: "satellite",
    track: "live",
    presetId: "weather-smoke-core-v1",
    layerIds: ["base-dark", "satellite", "smoke", "radar-rain"],
    bakedLayerIds: ["base-dark", "satellite", "smoke", "radar-rain"],
    eligibleOverlayLayerIds: ["model-mslp", "model-hgt500"],
    rangeHours: 3,
    cadenceMinutes: 10,
    viewport: { left: 0, top: 0, width: 1, height: 1 },
    mediaViewport: { left: 0, top: 0, width: 1, height: 1 },
    endValidTime: "2026-08-21T12:10:00Z",
    endSourceTime: "2026-08-21T12:10:00Z",
    boundaryIntervalMultiplier: 4,
    frames: [
      hybridFrame(
        "2026-08-21T12:00:00Z",
        "2026-08-21T12:00:00Z",
        "2026-08-21T11:00:00Z",
      ),
      hybridFrame(
        "2026-08-21T12:10:00Z",
        "2026-08-21T12:06:00Z",
        "2026-08-21T12:00:00Z",
      ),
    ],
    renditions: [{ id: "high", media }],
    proxies: { [proxy.path]: proxy },
  });
  const converted = compositeLoopVideoManifest(parsed, "high");
  assert.ok(converted);
  const finalFrame = converted.manifest.frames[1];
  assert.equal(finalFrame.layerSourceTimes?.["radar-rain"], "2026-08-21T12:06:00Z");

  const sourceTimes = videoFrameSourceTimeMap(finalFrame);
  assert.equal(sourceTimes.get("radar-rain"), Date.parse("2026-08-21T12:06:00Z"));
  assert.equal(sourceTimes.get("model-mslp"), Date.parse("2026-08-21T12:00:00Z"));
  assert.equal(sourceTimes.get("model-hgt500"), Date.parse("2026-08-21T12:00:00Z"));
  assert.equal(
    Date.parse("2026-08-21T12:00:00Z") > (sourceTimes.get("radar-rain") ?? -Infinity) + 1_000,
    false,
  );
});

test("exposes stable layer-control targets for deterministic toggles", async () => {
  const viewer = await readFile(new URL("../app/radar-viewer.tsx", import.meta.url), "utf8");
  assert.match(viewer, /htmlFor=\{`layer-\$\{product\.id\}-\$\{layer\.id\}`\}/);
  assert.match(viewer, /aria-label=\{layerControlLabel\(layer\.id\)\}/);
  assert.match(viewer, /data-layer-id=\{layer\.id\}/);
  assert.match(viewer, /id=\{`layer-\$\{product\.id\}-\$\{layer\.id\}`\}/);
  assert.match(viewer, /data-layer-id="radar-rain"|data-layer-id=\{layer\.id\}/);
  assert.match(viewer, /window\.localStorage\.setItem\(VIEWER_PREFERENCES_KEY/);
  assert.match(viewer, /effectiveRangeHours\}h-/);
  assert.match(viewer, /live-edge\.json/);
  assert.match(viewer, /live-edge-layer/);
  assert.match(viewer, /liveEdgeHostRef\.current\.hidden = !isHotEdge/);
  assert.match(viewer, /className="live-edge-host" hidden=\{!showLiveEdge\}/);
  assert.doesNotMatch(viewer, /visibility: showLiveEdge \? "visible" : "hidden"/);
  assert.match(viewer, /const resetToNewestFrame = useCallback/);
  assert.match(viewer, /presentedVideoIndexRef\.current = NEWEST_FRAME/);
  assert.match(viewer, /const selectFrame = useCallback[\s\S]*?presentedVideoIndexRef\.current = index;[\s\S]*?setFrameIndex\(index\)/);
  assert.match(viewer, /onChange=\{\(event\) => selectFrame\(Number\(event\.target\.value\)\)\}/);
  assert.match(viewer, /setRangeHours\(hours\); resetToNewestFrame\(\)/);
  assert.match(viewer, /key=\{`status-\$\{product\.id\}-\$\{effectiveRangeHours\}h-\$\{videoModeReady \? "video" : "images"\}`\}/);
});

test("renders weather-app lightning bolts and wildfire flames from point frames", async () => {
  const viewer = await readFile(new URL("../app/radar-viewer.tsx", import.meta.url), "utf8");
  const pointData = await readFile(new URL("../app/point-data.ts", import.meta.url), "utf8");
  const styles = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(viewer, /"lightning-trail"\) return "lightning-points"/);
  assert.match(viewer, /"glm-lightning-trail"\) return "glm-lightning-points"/);
  assert.match(viewer, /"hotspots"\) return "hotspot-points"/);
  assert.match(viewer, /"active-fire-points"/);
  assert.match(viewer, /pointFrameReferences\([\s\S]*6 \* 60/);
  assert.match(viewer, /<ZapIcon \/>/);
  assert.match(viewer, /<FlameIcon highlighted \/>/);
  assert.match(viewer, /candidate\.pointReferences\.forEach\(\(reference\) => preloadPointFrame/);
  assert.match(viewer, /<LightningCanvas/);
  assert.match(viewer, /RASTER_LIGHTNING_OVERLAYS = true/);
  assert.match(viewer, /RASTER_FIRE_OVERLAYS = true/);
  assert.match(viewer, /derived lightning trails are compact transparent PNGs/);
  assert.match(viewer, /<FireCanvas/);
  assert.match(viewer, /data-marker-count=\{markers\.length\}/);
  assert.match(viewer, /lookaheadCount = 3/);
  assert.match(viewer, /preloadImageFrame/);
  assert.match(viewer, /lightningUrls/);
  assert.doesNotMatch(viewer, /&& !LIGHTNING_CONTROLLERS\.has\(layer\.id\)/);
  assert.match(viewer, /targetDomain === "north-pacific"/);
  assert.match(viewer, /BC_ON_NORTH_AMERICA_STYLE/);
  assert.match(viewer, /active-fire-marker/);
  assert.match(viewer, /clusterNotableFires/);
  assert.match(viewer, /status: Math\.max\(\.\.\.group\.map\(\(marker\) => marker\.status\)\)/);
  assert.match(viewer, /const activeColours = \["#ff8a4f", "#53be69", "#f4c73f", "#ef5239"\]/);
  assert.match(viewer, /U\.S\. \/ unavailable/);
  assert.match(viewer, /context\.fillText\(String\(marker\.count\)/);
  assert.match(viewer, /BCWS Wildfire of Note/);
  assert.match(viewer, /U\.S\. current ICS-209 large incident/);
  assert.match(viewer, /highlight === 0/);
  assert.doesNotMatch(viewer, /sizeHectares < 5_000|sizeHectares >= 5_000/);
  assert.match(viewer, /hotspot-fire-marker/);
  assert.match(viewer, /className="point-symbol-layer fire-canvas"/);
  assert.match(viewer, /Medium\/low-confidence smoke tint/);
  assert.match(viewer, /ecccFallbackPointReferences/);
  assert.match(viewer, /layerId === "westwx-visir"\) return "NOAA VIS\/IR"/);
  assert.doesNotMatch(viewer, /layerId === "daynight"/);
  assert.match(viewer, /pointDomain = domain\?\.layers\["active-fire-points"\]/);
  assert.match(viewer, /targetDomain === "north-america" \|\| targetDomain === "north-pacific"/);
  assert.doesNotMatch(viewer, /latestRollingPointFrameReferences/);
  assert.match(viewer, /resilientActiveFireFrameReferences/);
  assert.match(viewer, /usesRasterLightning\(product\)/);
  assert.match(viewer, /usesRasterFire\(product\)/);
  assert.match(viewer, /`\$\{baseId\}-region-\$\{regionKey\}`/);
  assert.match(viewer, /stageAligned: renderedLayerId\.includes\("-region-"\)/);
  assert.doesNotMatch(viewer, /lightning-arrival-layer/);
  assert.match(viewer, /usesRasterFire\(product\) && recipe\.id === "hotspots"/);
  assert.match(viewer, /createRadialGradient/);
  assert.match(viewer, /layerId\.startsWith\("westwx-"\)/);
  assert.match(pointData, /coordinateSpace\.origin === "top-left"/);
  assert.match(styles, /\.lightning-marker\.age-0/);
  assert.match(styles, /\.fire-marker\.age-2/);
  assert.match(styles, /\.active-fire-marker\.fire-notable/);
  assert.match(styles, /\.fire-count/);
  assert.match(styles, /\.hotspot-fire-marker svg/);
  assert.match(styles, /\.eccc-north-fallback/);
  assert.doesNotMatch(styles, /\.lightning-arrival-layer/);
  assert.doesNotMatch(styles, /lightning-raster-arrival/);
  assert.match(styles, /\.transmission-symbol[\s\S]*?border-top: 2px solid #fff/);
  assert.match(styles, /\.lightning-marker\.age-3 \{ color: #f6d451/);
});

test("keeps a compact desktop control rail and gives the map the remaining width", async () => {
  const viewer = await readFile(new URL("../app/radar-viewer.tsx", import.meta.url), "utf8");
  const styles = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(styles, /\.app-shell\s*\{[\s\S]*?width: 100%/);
  assert.match(styles, /\.app-shell\s*\{[\s\S]*?grid-template-columns: clamp\(205px, 14\.5vw, 236px\) minmax\(0, 1fr\)/);
  assert.match(styles, /\.viewer-grid\s*\{[\s\S]*?display: contents/);
  assert.match(styles, /\.map-column\s*\{[\s\S]*?display: contents/);
  assert.match(styles, /\.map-stage\s*\{[\s\S]*?grid-column: 2/);
  assert.match(styles, /\.map-stage\s*\{[\s\S]*?place-self: center/);
  assert.match(styles, /\.legend-rail\s*\{[\s\S]*?grid-column: 1/);
  assert.match(styles, /\.legend-rail\s*\{[\s\S]*?overflow: hidden/);
  assert.match(styles, /\.timeline-scrubber\s*\{[\s\S]*?grid-column: 1 \/ -1/);
  assert.match(styles, /width: min\(100%, var\(--map-max-width/);
  assert.match(viewer, /"--map-max-width": `calc\(\$\{mapAspect \* 100\}dvh/);
  assert.match(styles, /\.sidebar-layer-controls/);
  assert.match(styles, /\.layers-popover\s*\{[\s\S]*?position: absolute;[\s\S]*?inset: 0/);
  assert.match(styles, /\.layer-selector:hover \.layers-popover/);
  assert.match(viewer, /event\.currentTarget\.contains\(focused\)/);
  assert.match(viewer, /focused\.blur\(\)/);
  assert.match(styles, /\.product-switcher \.selector-options,[\s\S]*?\.range-selector \.selector-options\s*\{[\s\S]*?bottom: calc\(100% - 1px\)/);
  assert.match(styles, /\.video-surface-layer canvas\[hidden\]\s*\{[\s\S]*?display: none/);
  assert.match(styles, /\.product-switcher \.product-button,[\s\S]*?\.range-selector \.range-button\s*\{[\s\S]*?width: 100%/);
  assert.match(styles, /\.legend-content\s*\{[\s\S]*?border: 1px solid var\(--border-strong\)/);
  assert.doesNotMatch(styles, /\.active-layer-list\s*\{/);
  assert.match(styles, /\.product-switcher \.selector-current,[\s\S]*?\.range-selector \.selector-current\s*\{[\s\S]*?width: 100%/);
  assert.doesNotMatch(viewer, /activeLayerLabels\.map\(\(label\) =>/);
  assert.doesNotMatch(viewer, /className="active-layer-item"/);
  assert.match(viewer, /aria-label=\{`Region: \$\{product\.shortTitle\}`\}/);
  assert.match(viewer, /aria-label=\{`Time span:/);
  assert.doesNotMatch(viewer, /Mixed freshness|live-summary|freshnessClock/);
  assert.match(viewer, /product-switcher/);
  assert.match(viewer, /className="sources-drawer"/);
});

test("ships a runtime data configuration", async () => {
  const viewer = await readFile(new URL("../app/radar-viewer.tsx", import.meta.url), "utf8");
  const styles = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  const config = JSON.parse(await readFile(new URL("../public/config.json", import.meta.url), "utf8"));
  assert.equal(typeof config.catalogIndexUrl, "string");
  assert.equal(typeof config.catalogUrl, "string");
  assert.match(viewer, /config\.catalogIndexUrl, config\.fallbackCatalogUrl, config\.catalogUrl/);
  assert.match(viewer, /catalog\?\.catalogMode !== "index"/);
  assert.match(viewer, /Retrying automatically/);
  assert.match(viewer, /window\.setTimeout\(\(\) =>/);
  assert.match(viewer, /Catalog endpoints failed/);
  assert.match(styles, /@media \(max-width: 840px\)[\s\S]*\.product-switcher \.selector-options,[\s\S]*top: calc\(100% - 1px\)/);
  await access(new URL("../out/config.json", import.meta.url));
  await access(new URL("../out/demo/catalog.json", import.meta.url));
  const demo = JSON.parse(await readFile(new URL("../public/demo/catalog.json", import.meta.url), "utf8"));
  assert.match(demo.assetBaseUrl, /^https:\/\//);
  const southCoast = demo.products.find((product) => product.id === "bc-south-coast-overlay");
  assert.equal(southCoast.title, "South Coast");
  assert.equal(southCoast.shortTitle, "South Coast");
  assert.deepEqual(southCoast.viewport, { left: 0.4923, top: 0.6929, width: 0.1612, height: 0.1362 });
  assert.equal(demo.domains.bc.layers.hotspots.maxAgeMinutes, 360);
  assert.equal(demo.domains.bc.layers["hotspots-region-south-coast"].maxAgeMinutes, 360);
  assert.ok(demo.domains.bc.layers["raw-visir"].frames.length > 0);
  const overlay = demo.products.find((product) => product.id === "bc-large-overlay");
  const small = demo.products.find((product) => product.id === "bc-small-overlay");
  assert.equal(overlay.shortTitle, "BC XL");
  assert.equal(small.shortTitle, "BC");
  assert.equal(small.anchorLayer, "eccc-geocolor");
  assert.equal(small.frameIntervalMinutes, 10);
  assert.equal(small.dayFrameIntervalMinutes, 30);
  assert.equal(small.archiveFrameIntervalMinutes, 60);
  assert.equal(small.maxHours, 168);
  assert.deepEqual(overlay.viewport, { left: 0, top: 0.05, width: 1, height: 0.9 });
  assert.deepEqual(small.viewport, { left: 0.16404, top: 0.22489, width: 0.670919, height: 0.581179 });
  assert.equal(overlay.anchorLayer, "eccc-geocolor");
  assert.equal(overlay.layers.find((layer) => layer.id === "eccc-geocolor").defaultEnabled, true);
  assert.equal(overlay.layers.some((layer) => layer.id === "daynight"), false);
  assert.equal(overlay.layers.some((layer) => layer.id === "ir"), false);
  assert.equal(overlay.layers.find((layer) => layer.id === "convective").optional, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "hotspots").optional, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "hotspots").defaultEnabled, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "raw-ir").choiceGroup, "satellite");
  assert.equal(overlay.layers.find((layer) => layer.id === "raw-visir").controlId, "noaa-visir");
  assert.equal(overlay.layers.find((layer) => layer.id === "raw-ir").controlId, "noaa-ir");
  assert.equal(overlay.layers.find((layer) => layer.id === "convective").controlSection, "regional-satellite");
  assert.equal(overlay.layers.find((layer) => layer.id === "lightning-trail").controlId, "lightning");
  assert.deepEqual(
    overlay.layers.filter((layer) => layer.choiceGroup === "satellite").map((layer) => layer.id),
    ["eccc-geocolor", "raw-visir", "raw-ir", "convective", "snowfog"],
  );
  assert.equal(overlay.layers.find((layer) => layer.id === "raw-visir").defaultEnabled, false);
  assert.match(viewer, /layerId === "eccc-geocolor"\) return "MSC GeoColor"/);
  assert.equal(overlay.layers.find((layer) => layer.id === "snowfog").defaultEnabled, false);
  assert.equal(overlay.layers.find((layer) => layer.id === "model-hgt500").defaultEnabled, false);
  assert.equal(overlay.layers.find((layer) => layer.id === "model-mslp").optional, true);
  assert.equal(overlay.legends.includes("model-hgt500"), false);
  assert.equal(overlay.legends.includes("model-mslp"), false);
  assert.match(viewer, /MSLP \+ 500 hPa/);
  assert.match(viewer, /ECMWF IFS Control/);
  assert.equal(demo.products.some((product) => product.group === "Snow / fog"), false);
  assert.equal(overlay.layers.find((layer) => layer.id === "ptype").choiceGroup, "precipitation");
  assert.equal(demo.domains.bc.staticLayers.watersheds.path, "static/bc/bch-watersheds.png");
  assert.match(viewer, /\["watersheds", "transmission-lines", "boundaries"\]\.includes\(recipe\.id\)/);
  assert.match(viewer, /`\$\{recipe\.id\}-region-\$\{regionKey\}`/);
  assert.match(viewer, /stageAligned: regionalStaticId\.includes\("-region-"\)/);
  assert.equal(demo.domains.bc.staticLayers["transmission-lines"].path, "static/bc/transmission-lines.png");
  assert.equal(overlay.legends.includes("transmission-lines"), true);
  assert.match(overlay.notes.join(" "), /54-polygon BC Hydro boundary source/);
  assert.equal(demo.products.some((product) => product.id === "bc-lightning"), false);
  assert.equal(demo.products.some((product) => product.id === "north-america-overlay"), true);
  assert.equal(demo.products.some((product) => product.id === "north-pacific-overlay"), true);
  const pacificWna = demo.products.find((product) => product.id === "pacific-wna-overlay");
  assert.equal(pacificWna.shortTitle, "Pacific/WNA");
  assert.deepEqual(pacificWna.viewport, { left: 0.21, top: 0.1479, width: 0.65, height: 0.7117 });
  const northAmerica = demo.products.find((product) => product.id === "north-america-overlay");
  const northPacific = demo.products.find((product) => product.id === "north-pacific-overlay");
  assert.equal(northPacific.shortTitle, "Pacific");
  assert.equal(demo.domains["north-pacific"].title, "Pacific");
  assert.equal(northAmerica.anchorLayer, "westwx-ir");
  assert.equal(northAmerica.frameIntervalMinutes, 20);
  assert.equal(northAmerica.dayFrameIntervalMinutes, 30);
  assert.equal(northAmerica.archiveFrameIntervalMinutes, 60);
  assert.deepEqual(northAmerica.viewport, { left: 0, top: 0.1763, width: 0.86, height: 0.78 });
  assert.deepEqual(
    northAmerica.layers.filter((layer) => layer.choiceGroup === "satellite").map((layer) => layer.id),
    ["westwx-visir", "westwx-ir"],
  );
  assert.equal(northAmerica.layers.find((layer) => layer.id === "westwx-visir").controlId, "noaa-visir");
  assert.equal(northAmerica.layers.find((layer) => layer.id === "westwx-ir").controlId, "noaa-ir");
  assert.equal(northAmerica.layers.find((layer) => layer.id === "glm-lightning-trail").controlId, "lightning");
  assert.match(viewer, /normalizeLayerChoices/);
  assert.match(viewer, /if \(controlId !== "model-contours"\) continue/);
  assert.doesNotMatch(viewer, /if \(!controlId\) continue;[\s\S]{0,500}sharedControls\.set/);
  assert.match(viewer, /Additional BC satellite/);
  assert.doesNotMatch(viewer, /Additional MSC\/ECCC satellite views are available/);
  assert.equal(northAmerica.layers.find((layer) => layer.id === "hotspots").defaultEnabled, true);
  assert.equal(northAmerica.layers.find((layer) => layer.id === "model-hgt500").optional, true);
  assert.equal(northAmerica.legends.includes("hotspots"), true);
  assert.equal(northPacific.anchorLayer, "raw-ir");
  assert.equal(northPacific.frameIntervalMinutes, 20);
  assert.match(viewer, /"lightning-hour"/);
  assert.match(viewer, /"glm-lightning-hour"/);
  assert.deepEqual(northPacific.viewport, { left: 0, top: 0.075936, width: 0.77, height: 0.9 });
  assert.equal(northPacific.layers.find((layer) => layer.id === "ptype").choiceGroup, "precipitation");
  assert.equal(northPacific.layers.find((layer) => layer.id === "hotspots").defaultEnabled, true);
  assert.ok(
    overlay.layers.findIndex((layer) => layer.id === "lightning-trail")
      > overlay.layers.findIndex((layer) => layer.id === "transmission-lines"),
  );
  assert.ok(
    overlay.layers.findIndex((layer) => layer.id === "hotspots")
      > overlay.layers.findIndex((layer) => layer.id === "watersheds"),
  );
  assert.ok(
    overlay.layers.findIndex((layer) => layer.id === "model-mslp")
      > overlay.layers.findIndex((layer) => layer.id === "hotspots"),
  );
  assert.ok(
    northPacific.layers.findIndex((layer) => layer.id === "glm-lightning-trail")
      > northPacific.layers.findIndex((layer) => layer.id === "transmission-lines"),
  );
  assert.equal(
    northPacific.layers.find((layer) => layer.id === "lightning-trail").enabledWith,
    "glm-lightning-trail",
  );
  assert.ok(
    northPacific.layers.findIndex((layer) => layer.id === "model-mslp")
      < northPacific.layers.findIndex((layer) => layer.id === "model-hgt500"),
  );
  assert.ok(
    northPacific.layers.findIndex((layer) => layer.id === "model-mslp")
      > northPacific.layers.findIndex((layer) => layer.id === "hotspots"),
  );
});

test("deploy workflow uses the GitHub Pages artifact flow", async () => {
  const workflow = await readFile(new URL("../.github/workflows/pages.yml", import.meta.url), "utf8");
  assert.match(workflow, /npm run build:pages/);
  assert.match(workflow, /actions\/upload-pages-artifact@v3/);
  assert.match(workflow, /actions\/deploy-pages@v4/);
});
