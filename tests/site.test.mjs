import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

test("exports the operational viewer", async () => {
  const html = await readFile(new URL("../out/index.html", import.meta.url), "utf8");
  assert.match(html, /BC Satellite\/Radar\/Lightning/);
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
  assert.match(viewer, /PLAYBACK_SPEEDS = \[0\.5, 0\.75, 1, 1\.5, 2, 3, 4, 5\]/);
  assert.match(viewer, /setPlaying\(true\)/);
  assert.match(viewer, /activeAnchorLayer/);
});

test("renders weather-app lightning bolts and wildfire flames from point frames", async () => {
  const viewer = await readFile(new URL("../app/radar-viewer.tsx", import.meta.url), "utf8");
  const pointData = await readFile(new URL("../app/point-data.ts", import.meta.url), "utf8");
  const styles = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(viewer, /"lightning-trail"\) return "lightning-points"/);
  assert.match(viewer, /"glm-lightning-trail"\) return "glm-lightning-points"/);
  assert.match(viewer, /"hotspots"\) return "hotspot-points"/);
  assert.match(viewer, /pointFrameReferences\([\s\S]*6 \* 60/);
  assert.match(viewer, /<ZapIcon \/>/);
  assert.match(viewer, /<FlameIcon \/>/);
  assert.match(viewer, /nextPointReferences\.forEach\(\(reference\) => preloadPointFrame/);
  assert.match(viewer, /BC_ON_NORTH_AMERICA_STYLE/);
  assert.match(viewer, /ecccFallbackPointReferences/);
  assert.match(viewer, /layerId === "westwx-visir"\) return "VisIR Blend"/);
  assert.match(viewer, /layerId\.startsWith\("westwx-"\)/);
  assert.match(pointData, /coordinateSpace\.origin === "top-left"/);
  assert.match(styles, /\.lightning-marker\.age-0/);
  assert.match(styles, /\.fire-marker\.age-2/);
  assert.match(styles, /\.eccc-north-fallback/);
  assert.match(styles, /@keyframes lightning-arrival/);
});

test("ships a runtime data configuration", async () => {
  const config = JSON.parse(await readFile(new URL("../public/config.json", import.meta.url), "utf8"));
  assert.equal(typeof config.catalogUrl, "string");
  await access(new URL("../out/config.json", import.meta.url));
  await access(new URL("../out/demo/catalog.json", import.meta.url));
  const demo = JSON.parse(await readFile(new URL("../public/demo/catalog.json", import.meta.url), "utf8"));
  const overlay = demo.products.find((product) => product.id === "bc-large-overlay");
  const small = demo.products.find((product) => product.id === "bc-small-overlay");
  assert.equal(overlay.shortTitle, "BC Large");
  assert.equal(small.shortTitle, "BC Small");
  assert.equal(overlay.layers.find((layer) => layer.id === "daynight").defaultEnabled, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "convective").optional, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "hotspots").optional, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "hotspots").defaultEnabled, true);
  assert.equal(overlay.layers.find((layer) => layer.id === "raw-visible").choiceGroup, "satellite");
  assert.equal(overlay.layers.find((layer) => layer.id === "raw-ir").choiceGroup, "satellite");
  assert.equal(overlay.layers.find((layer) => layer.id === "ptype").choiceGroup, "precipitation");
  assert.equal(demo.domains.bc.staticLayers.watersheds.path, "static/bc/bch-watersheds.png");
  assert.match(overlay.notes.join(" "), /54-polygon BC Hydro boundary source/);
  assert.equal(demo.products.some((product) => product.id === "bc-lightning"), false);
  assert.equal(demo.products.some((product) => product.id === "north-america-overlay"), true);
  assert.equal(demo.products.some((product) => product.id === "north-pacific-overlay"), true);
  const northAmerica = demo.products.find((product) => product.id === "north-america-overlay");
  const northPacific = demo.products.find((product) => product.id === "north-pacific-overlay");
  assert.equal(northAmerica.anchorLayer, "westwx-ir");
  assert.deepEqual(
    northAmerica.layers.filter((layer) => layer.choiceGroup === "satellite").map((layer) => layer.id),
    ["westwx-visir", "westwx-visible", "westwx-ir"],
  );
  assert.equal(northAmerica.layers.find((layer) => layer.id === "hotspots").defaultEnabled, true);
  assert.equal(northAmerica.legends.includes("hotspots"), true);
  assert.equal(northPacific.anchorLayer, "raw-ir");
});

test("deploy workflow uses the GitHub Pages artifact flow", async () => {
  const workflow = await readFile(new URL("../.github/workflows/pages.yml", import.meta.url), "utf8");
  assert.match(workflow, /npm run build:pages/);
  assert.match(workflow, /actions\/upload-pages-artifact@v3/);
  assert.match(workflow, /actions\/deploy-pages@v4/);
});
