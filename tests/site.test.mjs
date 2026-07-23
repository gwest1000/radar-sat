import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

test("exports the operational viewer", async () => {
  const html = await readFile(new URL("../out/index.html", import.meta.url), "utf8");
  assert.match(html, /BC Satellite\/Radar\/Lightning\/Fires/);
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
  assert.match(viewer, /Promise\.all\(loads\)/);
  assert.match(viewer, /first-pass playback can slow down but never flashes a blank frame/);
  assert.match(viewer, /atOrBeforeSourceTime/);
  assert.match(viewer, /sourceCount > selectedSourceCount/);
  assert.match(viewer, /PLAYBACK_SPEEDS = \[0\.25, 0\.5, 0\.75, 1, 1\.25, 1\.5, 1\.75, 2\]/);
  assert.match(viewer, /useState\(3\)/);
  assert.match(viewer, /\? stored\.speedIndex\s*: 3/);
  assert.match(viewer, /finalFrame \? 400 \/ speed : 100 \/ speed/);
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
  assert.match(viewer, /nextPointReferences\.forEach\(\(reference\) => preloadPointFrame/);
  assert.match(viewer, /BC_ON_NORTH_AMERICA_STYLE/);
  assert.match(viewer, /active-fire-marker/);
  assert.match(viewer, /clusterNotableFires/);
  assert.match(viewer, /className="fire-count"/);
  assert.match(viewer, /BCWS Wildfire of Note/);
  assert.match(viewer, /U\.S\. current ICS-209 large incident/);
  assert.match(viewer, /highlight === 0/);
  assert.doesNotMatch(viewer, /sizeHectares < 5_000|sizeHectares >= 5_000/);
  assert.match(viewer, /hotspot-fire-marker/);
  assert.match(viewer, /<FlameIcon filled=\{marker\.kind === "active"\} highlighted=\{marker\.notable\} \/>/);
  assert.match(viewer, /Low-confidence detection/);
  assert.match(viewer, /ecccFallbackPointReferences/);
  assert.match(viewer, /layerId === "westwx-visir"\) return "NOAA VIS\/IR"/);
  assert.match(viewer, /layerId === "daynight"\) return "ECCC VIS\/IR"/);
  assert.match(viewer, /pointDomain = domain\?\.layers\["active-fire-points"\]/);
  assert.match(viewer, /layerId\.startsWith\("westwx-"\)/);
  assert.match(pointData, /coordinateSpace\.origin === "top-left"/);
  assert.match(styles, /\.lightning-marker\.age-0/);
  assert.match(styles, /\.fire-marker\.age-2/);
  assert.match(styles, /\.active-fire-marker\.fire-notable/);
  assert.match(styles, /\.fire-count/);
  assert.match(styles, /\.hotspot-fire-marker svg/);
  assert.match(styles, /\.eccc-north-fallback/);
  assert.doesNotMatch(styles, /@keyframes lightning-arrival/);
  assert.match(styles, /\.lightning-marker\.age-3 \{ color: #f6d451/);
});

test("keeps the desktop controls and map at the full available width", async () => {
  const viewer = await readFile(new URL("../app/radar-viewer.tsx", import.meta.url), "utf8");
  const styles = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(styles, /\.app-shell\s*\{[\s\S]*?width: 100%/);
  assert.match(styles, /\.viewer-grid\s*\{[\s\S]*?grid-template-columns: minmax\(0, 1fr\) 190px/);
  assert.match(styles, /width: min\(100%, var\(--map-max-width/);
  assert.match(viewer, /"--map-max-width": `calc\(\$\{mapAspect \* 100\}vh/);
  assert.match(styles, /\.sidebar-layer-controls/);
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
  assert.equal(overlay.anchorLayer, "raw-visir");
  assert.equal(overlay.layers.find((layer) => layer.id === "raw-visir").defaultEnabled, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "daynight").defaultEnabled, false);
  assert.equal(overlay.layers.find((layer) => layer.id === "convective").optional, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "hotspots").optional, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "hotspots").defaultEnabled, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "raw-ir").choiceGroup, "satellite");
  assert.deepEqual(
    overlay.layers.filter((layer) => layer.choiceGroup === "satellite").map((layer) => layer.id),
    ["raw-visir", "raw-ir", "daynight", "ir", "convective"],
  );
  assert.equal(overlay.layers.find((layer) => layer.id === "ptype").choiceGroup, "precipitation");
  assert.equal(demo.domains.bc.staticLayers.watersheds.path, "static/bc/bch-watersheds.png");
  assert.match(overlay.notes.join(" "), /54-polygon BC Hydro boundary source/);
  assert.equal(demo.products.some((product) => product.id === "bc-lightning"), false);
  assert.equal(demo.products.some((product) => product.id === "north-america-overlay"), true);
  assert.equal(demo.products.some((product) => product.id === "north-pacific-overlay"), true);
  assert.equal(demo.products.find((product) => product.id === "pacific-wna-overlay").shortTitle, "Pacific/WNA");
  const northAmerica = demo.products.find((product) => product.id === "north-america-overlay");
  const northPacific = demo.products.find((product) => product.id === "north-pacific-overlay");
  assert.equal(northAmerica.anchorLayer, "westwx-ir");
  assert.deepEqual(
    northAmerica.layers.filter((layer) => layer.choiceGroup === "satellite").map((layer) => layer.id),
    ["westwx-visir", "westwx-ir"],
  );
  assert.equal(northAmerica.layers.find((layer) => layer.id === "hotspots").defaultEnabled, true);
  assert.equal(northAmerica.legends.includes("hotspots"), true);
  assert.equal(northPacific.anchorLayer, "raw-ir");
  assert.equal(northPacific.layers.find((layer) => layer.id === "ptype").choiceGroup, "precipitation");
  assert.equal(northPacific.layers.find((layer) => layer.id === "hotspots").defaultEnabled, true);
});

test("deploy workflow uses the GitHub Pages artifact flow", async () => {
  const workflow = await readFile(new URL("../.github/workflows/pages.yml", import.meta.url), "utf8");
  assert.match(workflow, /npm run build:pages/);
  assert.match(workflow, /actions\/upload-pages-artifact@v3/);
  assert.match(workflow, /actions\/deploy-pages@v4/);
});
