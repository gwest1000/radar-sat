import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

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
  assert.match(viewer, /function playbackFrames/);
  assert.match(viewer, /SAME_SLOT_TOLERANCE_MS/);
  assert.match(viewer, /const frame = selectedFrame \?\?/);
  assert.match(viewer, /product\.frameIntervalMinutes/);
  assert.match(viewer, /product\.dayFrameIntervalMinutes/);
  assert.match(viewer, /product\.archiveFrameIntervalMinutes/);
  assert.match(viewer, /return playbackFrames\([\s\S]*?product\.frameIntervalMinutes,[\s\S]*?product\.dayFrameIntervalMinutes,[\s\S]*?product\.archiveFrameIntervalMinutes/);
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
  assert.match(viewer, /validLineRef\.current\.textContent/);
  assert.doesNotMatch(viewer, /setFrameIndex\(\(current\) => current === index \? current : index\)/);
  assert.match(videoLoop, /transport: "progressive-mp4"/);
  assert.match(videoLoop, /track: "live" \| "day" \| "archive"/);
  assert.match(videoLoop, /mediaViewport/);
  assert.match(videoLoop, /proxies: Record<string, VideoProxy>/);
  assert.match(videoLoop, /proxyLayers: VideoProxyLayerSelection\[\]/);
  assert.match(videoLoop, /MAX_MANIFEST_CACHE_ENTRIES = 8/);
  assert.match(videoLoop, /manifestCache\.size > MAX_MANIFEST_CACHE_ENTRIES/);
  assert.match(videoLoop, /requestVideoFrameCallback/);
  assert.match(viewer, /for \(const selection of manifestFrame\.proxyLayers\)/);
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
  assert.match(compositor, /surfaceCache\.retain\(new Set\(plans\.map/);
  assert.match(compositor, /const readyToResume = committedIndexRef\.current < 0/);
  assert.match(compositor, /const delay = plans\[index\]\.frame\.durationSeconds \* 1_000 \/ speedRef\.current/);
  assert.match(compositor, /data-surface-cache-limit/);
  assert.match(compositor, /data-surface-builds/);
  assert.match(compositor, /if \(playingRef\.current\) return/);
  assert.match(compositor, /if \(speed >= 4\) return 12/);
  assert.match(compositor, /PLAYBACK_SURFACE_PIXELS = 1_300_000/);
  assert.match(compositor, /playbackSurfaceSize\(manifest\.width, manifest\.height\)/);
  assert.match(compositor, /crossOrigin="anonymous"/);
  assert.match(compositor, /requestVideoFrameCallback/);
  assert.match(compositor, /data-overlay-stalls="0"/);
  assert.match(videoLoop, /defaultComposite\?: VideoDefaultComposite/);
  assert.match(videoLoop, /validDefaultComposite/);
  assert.match(videoLoop, /return \{ \.\.\.parsed, defaultComposite: undefined \}/);
  assert.match(viewer, /sameLayerSet\(enabledVideoLayerIds, candidateDefaultComposite\.layerIds\)/);
  assert.match(viewer, /mediaViewport: activeDefaultComposite\.mediaViewport/);
  assert.match(viewer, /satelliteFilter=\{activeDefaultComposite \? undefined : satelliteFilter\}/);
  assert.match(viewer, /setFailedDefaultComposite/);
  assert.match(viewer, /data-composite-preset=/);
  assert.match(compositor, /const fullyComposited = Boolean\(compositePresetId\)/);
  assert.match(compositor, /fullyComposited \? EMPTY_PREPARED_SURFACES/);
  assert.match(compositor, /if \(!fullyComposited\) commitSurfaces\(prepared\)/);
  assert.match(compositor, /getVideoPlaybackQuality/);
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
  assert.match(viewer, /layerId === "daynight"\) return "ECCC VIS\/IR"/);
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
  assert.deepEqual(southCoast.viewport, { left: 0.4985, top: 0.6929, width: 0.155, height: 0.1362 });
  assert.ok(demo.domains.bc.layers["raw-visir"].frames.length > 0);
  const overlay = demo.products.find((product) => product.id === "bc-large-overlay");
  const small = demo.products.find((product) => product.id === "bc-small-overlay");
  assert.equal(overlay.shortTitle, "BC XL");
  assert.equal(small.shortTitle, "BC");
  assert.equal(small.anchorLayer, "raw-visir-5min");
  assert.equal(small.frameIntervalMinutes, 10);
  assert.equal(small.dayFrameIntervalMinutes, 30);
  assert.equal(small.archiveFrameIntervalMinutes, 60);
  assert.equal(small.maxHours, 168);
  assert.deepEqual(overlay.viewport, { left: 0, top: 0.05, width: 1, height: 0.9 });
  assert.deepEqual(small.viewport, { left: 0.16404, top: 0.22489, width: 0.670919, height: 0.581179 });
  assert.equal(overlay.anchorLayer, "raw-visir");
  assert.equal(overlay.layers.find((layer) => layer.id === "raw-visir").defaultEnabled, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "daynight").defaultEnabled, false);
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
    ["raw-visir", "raw-ir", "eccc-geocolor", "daynight", "ir", "convective", "snowfog"],
  );
  assert.equal(overlay.layers.find((layer) => layer.id === "eccc-geocolor").defaultEnabled, false);
  assert.match(viewer, /layerId === "eccc-geocolor"\) return "MSC GeoColor"/);
  assert.equal(overlay.layers.find((layer) => layer.id === "snowfog").defaultEnabled, false);
  assert.equal(overlay.layers.find((layer) => layer.id === "model-hgt500").defaultEnabled, false);
  assert.equal(overlay.layers.find((layer) => layer.id === "model-mslp").optional, true);
  assert.equal(overlay.legends.includes("model-hgt500"), false);
  assert.equal(overlay.legends.includes("model-mslp"), false);
  assert.match(viewer, /500 hPa Height/);
  assert.match(viewer, /ECMWF IFS Control/);
  assert.equal(demo.products.some((product) => product.group === "Snow / fog"), false);
  assert.equal(overlay.layers.find((layer) => layer.id === "ptype").choiceGroup, "precipitation");
  assert.equal(demo.domains.bc.staticLayers.watersheds.path, "static/bc/bch-watersheds.png");
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
