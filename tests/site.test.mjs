import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

test("exports the operational viewer", async () => {
  const html = await readFile(new URL("../out/index.html", import.meta.url), "utf8");
  assert.match(html, /Real-Time WX Display/);
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
  assert.match(viewer, /FIVE_MINUTES_MS = 5 \* 60_000/);
  assert.match(viewer, /THIRTY_MINUTES_MS = 30 \* 60_000/);
  assert.match(viewer, /return playbackFrames\(frames, effectiveRangeHours\)/);
  assert.doesNotMatch(viewer, /Promise\.all\(loads\)/);
  assert.match(viewer, /flashDisplayAge < FIVE_MINUTES_MS/);
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
  assert.match(viewer, /IMAGE_FRAME_CACHE_LIMIT = 96/);
  assert.match(viewer, /function StableMapImage/);
  assert.match(viewer, /setDisplayedSrc\(src\)/);
  assert.match(viewer, /atOrBefore\(nativeLayer\?\.frames \?\? \[\], anchor\.validTime/);
  assert.match(viewer, /setPlaying\(true\)/);
  assert.match(viewer, /activeAnchorLayer/);
  assert.match(viewer, /AUTO_REFRESH_MS = 5 \* 60_000/);
  assert.match(viewer, /document\.visibilityState !== "visible"/);
  assert.match(viewer, /window\.location\.reload\(\)/);
  assert.match(viewer, /VIEWER_PREFERENCES_KEY/);
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
  assert.match(viewer, /typically only[\s\S]*7–12 KB/);
  assert.match(viewer, /<FireCanvas/);
  assert.match(viewer, /data-marker-count=\{markers\.length\}/);
  assert.match(viewer, /lookaheadCount = 6/);
  assert.match(viewer, /preloadImageFrame/);
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
  assert.match(viewer, /`\$\{recipeId\}-region-\$\{regionKey\}`/);
  assert.match(viewer, /`lightning-flash-region-\$\{regionKey\}`/);
  assert.match(viewer, /stageAligned: renderedLayerId\.includes\("-region-"\)/);
  assert.match(viewer, /lightning-arrival-layer/);
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
  assert.match(styles, /\.lightning-arrival-layer/);
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
  assert.match(styles, /\.product-switcher \.selector-options,[\s\S]*?\.range-selector \.selector-options\s*\{[\s\S]*?bottom: calc\(100% \+ 4px\)/);
  assert.match(styles, /\.product-switcher \.product-button,[\s\S]*?\.range-selector \.range-button\s*\{[\s\S]*?width: 100%/);
  assert.match(styles, /\.legend-content\s*\{[\s\S]*?border: 1px solid var\(--border-strong\)/);
  assert.match(styles, /\.active-layer-list\s*\{[\s\S]*?display: grid/);
  assert.match(styles, /\.product-switcher \.selector-current,[\s\S]*?\.range-selector \.selector-current\s*\{[\s\S]*?width: 100%/);
  assert.match(viewer, /activeLayerLabels\.map\(\(label\) =>/);
  assert.match(viewer, /className="active-layer-item"/);
  assert.match(viewer, /aria-label=\{`Region: \$\{product\.shortTitle\}`\}/);
  assert.match(viewer, /aria-label=\{`Time span:/);
  assert.doesNotMatch(viewer, /Mixed freshness|live-summary|freshnessClock/);
  assert.match(viewer, /product-switcher/);
  assert.match(viewer, /className="sources-drawer"/);
});

test("ships a runtime data configuration", async () => {
  const config = JSON.parse(await readFile(new URL("../public/config.json", import.meta.url), "utf8"));
  assert.equal(typeof config.catalogUrl, "string");
  await access(new URL("../out/config.json", import.meta.url));
  await access(new URL("../out/demo/catalog.json", import.meta.url));
  const demo = JSON.parse(await readFile(new URL("../public/demo/catalog.json", import.meta.url), "utf8"));
  const overlay = demo.products.find((product) => product.id === "bc-large-overlay");
  const small = demo.products.find((product) => product.id === "bc-small-overlay");
  assert.equal(overlay.shortTitle, "BC XL");
  assert.equal(small.shortTitle, "BC");
  assert.equal(small.anchorLayer, "raw-visir-5min");
  assert.equal(small.maxHours, 24);
  assert.deepEqual(overlay.viewport, { left: 0, top: 0.025, width: 1, height: 0.95 });
  assert.deepEqual(small.viewport, { left: 0.107, top: 0.155, width: 0.785, height: 0.68 });
  assert.equal(overlay.anchorLayer, "raw-visir");
  assert.equal(overlay.layers.find((layer) => layer.id === "raw-visir").defaultEnabled, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "daynight").defaultEnabled, false);
  assert.equal(overlay.layers.find((layer) => layer.id === "convective").optional, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "hotspots").optional, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "hotspots").defaultEnabled, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "raw-ir").choiceGroup, "satellite");
  assert.deepEqual(
    overlay.layers.filter((layer) => layer.choiceGroup === "satellite").map((layer) => layer.id),
    ["raw-visir", "raw-ir", "daynight", "ir", "convective", "snowfog"],
  );
  assert.equal(overlay.layers.find((layer) => layer.id === "snowfog").defaultEnabled, false);
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
  assert.deepEqual(pacificWna.viewport, { left: 0.2341, top: 0.1479, width: 0.6076, height: 0.7117 });
  const northAmerica = demo.products.find((product) => product.id === "north-america-overlay");
  const northPacific = demo.products.find((product) => product.id === "north-pacific-overlay");
  assert.equal(northPacific.shortTitle, "Pacific");
  assert.equal(demo.domains["north-pacific"].title, "Pacific");
  assert.equal(northAmerica.anchorLayer, "westwx-ir");
  assert.deepEqual(northAmerica.viewport, { left: 0, top: 0.1763, width: 0.8772, height: 0.8237 });
  assert.deepEqual(
    northAmerica.layers.filter((layer) => layer.choiceGroup === "satellite").map((layer) => layer.id),
    ["westwx-visir", "westwx-ir"],
  );
  assert.equal(northAmerica.layers.find((layer) => layer.id === "hotspots").defaultEnabled, true);
  assert.equal(northAmerica.legends.includes("hotspots"), true);
  assert.equal(northPacific.anchorLayer, "raw-ir");
  assert.deepEqual(northPacific.viewport, { left: 0, top: 0.075936, width: 0.735882, height: 0.924064 });
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
    northPacific.layers.findIndex((layer) => layer.id === "glm-lightning-trail")
      > northPacific.layers.findIndex((layer) => layer.id === "transmission-lines"),
  );
});

test("deploy workflow uses the GitHub Pages artifact flow", async () => {
  const workflow = await readFile(new URL("../.github/workflows/pages.yml", import.meta.url), "utf8");
  assert.match(workflow, /npm run build:pages/);
  assert.match(workflow, /actions\/upload-pages-artifact@v3/);
  assert.match(workflow, /actions\/deploy-pages@v4/);
});
