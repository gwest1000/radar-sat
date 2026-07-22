"use client";

import { CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { loadPointFrame, preloadPointFrame } from "./point-data";

type Frame = {
  validTime: string;
  path: string;
  source: string;
  sourceLayer: string;
  fetchedAt: string;
  sourceTimes?: Record<string, string>;
  detectionCount?: number;
  newestDetectionTime?: string | null;
  availability?: string;
  lowConfidencePixels?: number;
  mediumConfidencePixels?: number;
  highConfidencePixels?: number;
  mappedFlashCount?: number;
  pointCount?: number;
};

type PointFrameReference = {
  validTime: string;
  url: string;
  ageMinutes: number;
  frame: Frame;
};

type LightningMarker = {
  id: string;
  x: number;
  y: number;
  age: 0 | 1 | 2 | 3;
};

type FireMarker = {
  id: string;
  x: number;
  y: number;
  age: 0 | 1 | 2;
  kind: "active" | "hotspot";
  notable: boolean;
  highlight: 0 | 1 | 2;
  signal: number;
  count: number;
};

type ViewerPreferences = {
  productId: string;
  speedIndex: number;
  rangeHours: number;
  optionalLayers: Record<string, boolean>;
};

type DynamicLayer = {
  title?: string;
  maxAgeMinutes?: number;
  frames: Frame[];
};

type Domain = {
  id: string;
  title: string;
  width: number;
  height: number;
  projection: string;
  layers: Record<string, DynamicLayer>;
  staticLayers: Record<string, { path: string }>;
};

type ProductLayer = {
  id: string;
  opacity: number;
  optional?: boolean;
  defaultEnabled?: boolean;
  choiceGroup?: string;
  enabledWith?: string;
};

type Viewport = {
  left: number;
  top: number;
  width: number;
  height: number;
};

type Product = {
  id: string;
  title: string;
  shortTitle: string;
  group: string;
  domain: string;
  anchorLayer: string;
  defaultHours: number;
  description: string;
  layers: ProductLayer[];
  legends: string[];
  notes: string[];
  viewport?: Viewport;
};

type Legend = {
  title: string;
  path?: string;
  kind?: string;
};

type Catalog = {
  schemaVersion: number;
  generatedAt: string;
  domains: Record<string, Domain>;
  products: Product[];
  legends: Record<string, Legend>;
};

type SiteConfig = {
  catalogUrl: string;
  fallbackCatalogUrl?: string;
};

const RANGE_OPTIONS = [3, 6, 12, 24, 168];
const PLAYBACK_SPEEDS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
const AUTO_REFRESH_MS = 5 * 60_000;
const VIEWER_PREFERENCES_KEY = "radar-sat-viewer-preferences-v2";
const NEWEST_FRAME = Number.MAX_SAFE_INTEGER;
const FULL_VIEWPORT: Viewport = { left: 0, top: 0, width: 1, height: 1 };
const BC_ON_NORTH_AMERICA = { left: 0.269, top: 0.258, width: 0.286, height: 0.333 };
const BC_ON_NORTH_AMERICA_STYLE: CSSProperties = {
  left: `${BC_ON_NORTH_AMERICA.left * 100}%`,
  top: `${BC_ON_NORTH_AMERICA.top * 100}%`,
  width: `${BC_ON_NORTH_AMERICA.width * 100}%`,
  height: `${BC_ON_NORTH_AMERICA.height * 100}%`,
};
const LIGHTNING_CONTROLLERS = new Set(["lightning-trail", "glm-lightning-trail"]);

function pointLayerId(controllerId: string): string | undefined {
  if (controllerId === "lightning-trail") return "lightning-points";
  if (controllerId === "glm-lightning-trail") return "glm-lightning-points";
  if (controllerId === "hotspots") return "hotspot-points";
  return undefined;
}

function absoluteUrl(path: string, base: string): string {
  return new URL(path, base).toString();
}

function productHasFrames(catalog: Catalog, product: Product): boolean {
  return Boolean(catalog.domains[product.domain]?.layers[product.anchorLayer]?.frames?.length);
}

function frameUrl(frame: Frame, base: string): string {
  const url = new URL(frame.path, base);
  // A same-valid-time source can be corrected after first publication. The
  // fetch timestamp makes that replacement visible to a long-open browser.
  url.searchParams.set("v", frame.fetchedAt);
  return url.toString();
}

function pointFrameReferences(
  frames: Frame[],
  target: string,
  catalogBase: string,
  maxAgeMinutes: number,
  limit: number,
): PointFrameReference[] {
  const targetTime = Date.parse(target);
  if (!Number.isFinite(targetTime)) return [];
  return frames
    .filter((frame) => {
      const validTime = Date.parse(frame.validTime);
      return Number.isFinite(validTime)
        && validTime <= targetTime
        && targetTime - validTime <= maxAgeMinutes * 60_000;
    })
    .slice(-limit)
    .map((frame) => ({
      validTime: frame.validTime,
      url: frameUrl(frame, catalogBase),
      ageMinutes: Math.max(0, (targetTime - Date.parse(frame.validTime)) / 60_000),
      frame,
    }));
}

function rollingPointFrameReferences(
  frames: Frame[],
  target: string,
  catalogBase: string,
  maxAgeMinutes: number,
): PointFrameReference[] {
  const references = pointFrameReferences(frames, target, catalogBase, maxAgeMinutes, 1);
  if (references.length) return references;
  const latest = frames[frames.length - 1];
  const targetTime = Date.parse(target);
  const latestTime = latest ? Date.parse(latest.validTime) : Number.NaN;
  if (
    !latest
    || !Number.isFinite(targetTime)
    || !Number.isFinite(latestTime)
    || latestTime < targetTime
    || latestTime - targetTime > maxAgeMinutes * 60_000
  ) return [];
  return [{
    validTime: latest.validTime,
    url: frameUrl(latest, catalogBase),
    ageMinutes: 0,
    frame: latest,
  }];
}

function latestRollingPointFrameReferences(
  frames: Frame[],
  target: string,
  catalogBase: string,
  maxOffsetMinutes: number,
): PointFrameReference[] {
  const latest = frames[frames.length - 1];
  const targetTime = Date.parse(target);
  const latestTime = latest ? Date.parse(latest.validTime) : Number.NaN;
  if (
    !latest
    || !Number.isFinite(targetTime)
    || !Number.isFinite(latestTime)
    || Math.abs(latestTime - targetTime) > maxOffsetMinutes * 60_000
  ) return [];
  return [{
    validTime: latest.validTime,
    url: frameUrl(latest, catalogBase),
    ageMinutes: Math.max(0, (targetTime - latestTime) / 60_000),
    frame: latest,
  }];
}

async function buildLightningMarkers(
  references: PointFrameReference[],
  idPrefix = "",
): Promise<LightningMarker[]> {
  const frames = await Promise.all(references.map(async (reference) => ({
    reference,
    payload: await loadPointFrame(reference.url),
  })));
  const byLocation = new Map<string, LightningMarker>();
  for (const { reference, payload } of frames) {
    payload.points.forEach((point, index) => {
      const [x, y, pointAge = 0] = point;
      if (![x, y, pointAge].every(Number.isFinite) || x < 0 || x > 1 || y < 0 || y > 1) return;
      const totalAge = Math.max(0, reference.ageMinutes + pointAge);
      const age: LightningMarker["age"] = totalAge < 10 ? 0 : totalAge < 20 ? 1 : totalAge < 30 ? 2 : 3;
      const location = `${Math.round(x * 10_000)}-${Math.round(y * 10_000)}`;
      const marker = {
        id: `${idPrefix}${payload.domain}-${reference.validTime}-${index}`,
        x: x * 100,
        y: y * 100,
        age,
      } satisfies LightningMarker;
      const previous = byLocation.get(location);
      if (!previous || marker.age < previous.age) byLocation.set(location, marker);
    });
  }
  return [...byLocation.values()].sort((left, right) => right.age - left.age);
}

function remapFirePoint(
  x: number,
  y: number,
  sourceDomain: string,
  targetDomain: string,
): [number, number] | undefined {
  if (sourceDomain === targetDomain) return [x, y];
  if (sourceDomain === "north-america" && targetDomain === "bc") {
    const mappedX = (x - BC_ON_NORTH_AMERICA.left) / BC_ON_NORTH_AMERICA.width;
    const mappedY = (y - BC_ON_NORTH_AMERICA.top) / BC_ON_NORTH_AMERICA.height;
    if (mappedX < 0 || mappedX > 1 || mappedY < 0 || mappedY > 1) return undefined;
    return [mappedX, mappedY];
  }
  if (sourceDomain === "bc" && targetDomain === "north-america") {
    return [
      BC_ON_NORTH_AMERICA.left + x * BC_ON_NORTH_AMERICA.width,
      BC_ON_NORTH_AMERICA.top + y * BC_ON_NORTH_AMERICA.height,
    ];
  }
  return undefined;
}

function clusterNotableFires(markers: FireMarker[], targetDomain: string): FireMarker[] {
  if (targetDomain !== "bc") return markers;
  const regular = markers.filter((marker) => !marker.notable);
  const remaining = markers.filter((marker) => marker.notable);
  const clustered: FireMarker[] = [];
  const clusterDistance = 0.8;
  while (remaining.length) {
    const seed = remaining.shift();
    if (!seed) break;
    const group = [seed];
    for (let index = remaining.length - 1; index >= 0; index -= 1) {
      const candidate = remaining[index];
      if (
        candidate.highlight === seed.highlight
        && Math.hypot(candidate.x - seed.x, candidate.y - seed.y) < clusterDistance
      ) {
        group.push(candidate);
        remaining.splice(index, 1);
      }
    }
    if (group.length === 1) {
      clustered.push(seed);
      continue;
    }
    clustered.push({
      ...seed,
      id: `notable-cluster-${group.map((marker) => marker.id).join("-")}`,
      x: group.reduce((sum, marker) => sum + marker.x, 0) / group.length,
      y: group.reduce((sum, marker) => sum + marker.y, 0) / group.length,
      signal: Math.max(...group.map((marker) => marker.signal)),
      count: group.reduce((sum, marker) => sum + marker.count, 0),
    });
  }
  return [...regular, ...clustered];
}

async function buildFireMarkers(
  activeReference: PointFrameReference | undefined,
  hotspotReference: PointFrameReference | undefined,
  targetDomain: string,
): Promise<FireMarker[]> {
  const [activePayload, hotspotPayload] = await Promise.all([
    activeReference ? loadPointFrame(activeReference.url) : Promise.resolve(undefined),
    hotspotReference ? loadPointFrame(hotspotReference.url) : Promise.resolve(undefined),
  ]);
  const overview = targetDomain === "north-america";
  const activeMarkers = (activePayload?.points ?? []).flatMap((point, index): FireMarker[] => {
    const [x, y, , sizeValue, , highlightValue] = point;
    const sizeHectares = Number.isFinite(sizeValue) ? sizeValue : 0;
    const highlight: FireMarker["highlight"] = highlightValue === 1 || highlightValue === 2
      ? highlightValue
      : 0;
    if (![x, y].every(Number.isFinite) || x < 0 || x > 1 || y < 0 || y > 1) return [];
    if (overview && highlight === 0) return [];
    const mapped = remapFirePoint(x, y, activePayload?.domain ?? "north-america", targetDomain);
    if (!mapped) return [];
    return [{
      id: `active-${activeReference?.validTime}-${index}`,
      x: mapped[0] * 100,
      y: mapped[1] * 100,
      age: 0,
      kind: "active",
      notable: highlight > 0,
      highlight,
      signal: sizeHectares,
      count: 1,
    }];
  });
  const displayedActiveMarkers = clusterNotableFires(activeMarkers, targetDomain);
  const hotspotMarkers = (hotspotPayload?.points ?? []).flatMap((point, index): FireMarker[] => {
    const [x, y, pointAge = 0, frpValue] = point;
    const frp = Number.isFinite(frpValue) ? frpValue : 0;
    if (![x, y, pointAge].every(Number.isFinite) || x < 0 || x > 1 || y < 0 || y > 1) return [];
    if (overview && frp < 100) return [];
    const mapped = remapFirePoint(x, y, hotspotPayload?.domain ?? targetDomain, targetDomain);
    if (!mapped) return [];
    const duplicateDistance = targetDomain === "bc" ? 0.012 : 0.0035;
    if (displayedActiveMarkers.some((active) => {
      const dx = mapped[0] - active.x / 100;
      const dy = mapped[1] - active.y / 100;
      return dx * dx + dy * dy < duplicateDistance * duplicateDistance;
    })) return [];
    const totalAge = Math.max(0, (hotspotReference?.ageMinutes ?? 0) + pointAge);
    return [{
      id: `hotspot-${hotspotReference?.validTime}-${index}`,
      x: mapped[0] * 100,
      y: mapped[1] * 100,
      age: totalAge <= 6 * 60 ? 0 : totalAge <= 12 * 60 ? 1 : 2,
      kind: "hotspot",
      notable: false,
      highlight: 0,
      signal: frp,
      count: 1,
    }];
  });
  return [...hotspotMarkers, ...displayedActiveMarkers].sort((left, right) => (
    Number(left.notable) - Number(right.notable) || left.signal - right.signal
  ));
}

type ComposedLayer = {
  id: string;
  url: string;
  opacity: number;
  frame?: Frame;
};

function isProductLayerEnabled(
  recipe: ProductLayer,
  optionalLayers: Record<string, boolean>,
  recipes: ProductLayer[] = [],
): boolean {
  if (recipe.enabledWith) {
    const controller = recipes.find((candidate) => candidate.id === recipe.enabledWith);
    return Boolean(controller && isProductLayerEnabled(controller, optionalLayers, recipes));
  }
  if (!recipe.optional) return true;
  return optionalLayers[recipe.id] ?? recipe.defaultEnabled ?? true;
}

function activeAnchorLayer(product: Product, optionalLayers: Record<string, boolean>): string {
  const enabled = (id: string) => {
    const recipe = product.layers.find((candidate) => candidate.id === id);
    return Boolean(recipe && isProductLayerEnabled(recipe, optionalLayers, product.layers));
  };
  return ["raw-visible", "raw-visir", "raw-ir", "natural", "daynight", "ir", "convective", "radar-rain", "ptype", "lightning-trail", "hotspots"]
    .find(enabled) ?? product.anchorLayer;
}

function composeLayers(
  product: Product,
  domain: Domain,
  anchor: Frame,
  catalogBase: string,
  optionalLayers: Record<string, boolean>,
): ComposedLayer[] {
  return product.layers.flatMap((recipe) => {
    if (!isProductLayerEnabled(recipe, optionalLayers, product.layers)) return [];
    const pointsId = pointLayerId(recipe.id);
    if (pointsId && domain.layers[pointsId]?.frames?.length) return [];
    const staticLayer = domain.staticLayers[recipe.id];
    if (staticLayer) {
      return [{
        id: recipe.id,
        url: absoluteUrl(staticLayer.path, catalogBase),
        opacity: recipe.opacity,
      }];
    }
    const dynamicLayer = domain.layers[recipe.id];
    const frames = dynamicLayer?.frames ?? [];
    // Trail rasters are regenerated on the radar clock, but their actual
    // observations are ten-minute lightning intervals. Select them by that
    // source interval so VALID and the 0–10/10–20/20–30 minute bins agree.
    const frame = recipe.id.endsWith("lightning-trail")
      ? atOrBeforeSourceTime(recipe.id, frames, anchor.validTime, dynamicLayer?.maxAgeMinutes)
      : atOrBefore(frames, anchor.validTime, dynamicLayer?.maxAgeMinutes);
    if (!frame) return [];
    return [{
      id: recipe.id,
      url: frameUrl(frame, catalogBase),
      opacity: recipe.opacity,
      frame,
    }];
  });
}

function actualSourceTime(layerId: string, frame: Frame): string {
  if (layerId.endsWith("lightning-trail") && frame.sourceTimes) {
    const values = Object.values(frame.sourceTimes)
      .filter((value) => Number.isFinite(Date.parse(value)))
      .sort((left, right) => Date.parse(right) - Date.parse(left));
    if (values[0]) return values[0];
  }
  if ((layerId === "raw-visible" || layerId === "raw-ir") && frame.sourceTimes) {
    const values = Object.values(frame.sourceTimes)
      .filter((value) => Number.isFinite(Date.parse(value)))
      .sort((left, right) => Date.parse(right) - Date.parse(left));
    if (values[0]) return values[0];
  }
  return frame.validTime;
}

function atOrBefore(frames: Frame[], target: string, maxAgeMinutes?: number): Frame | undefined {
  const targetTime = Date.parse(target);
  if (!Number.isFinite(targetTime)) return undefined;
  let selected: Frame | undefined;
  for (const frame of frames) {
    if (Date.parse(frame.validTime) <= targetTime) selected = frame;
    else break;
  }
  if (selected && maxAgeMinutes !== undefined) {
    const ageMinutes = (targetTime - Date.parse(selected.validTime)) / 60_000;
    if (!Number.isFinite(ageMinutes) || ageMinutes > maxAgeMinutes) return undefined;
  }
  return selected;
}

function atOrBeforeSourceTime(
  layerId: string,
  frames: Frame[],
  target: string,
  maxAgeMinutes?: number,
): Frame | undefined {
  const targetTime = Date.parse(target);
  if (!Number.isFinite(targetTime)) return undefined;
  let selected: Frame | undefined;
  let selectedSourceTime = -Infinity;
  let selectedSourceCount = -1;
  for (const frame of frames) {
    const sourceTime = Date.parse(actualSourceTime(layerId, frame));
    const sourceCount = Object.keys(frame.sourceTimes ?? {}).length;
    if (!Number.isFinite(sourceTime) || sourceTime > targetTime) continue;
    if (
      sourceTime > selectedSourceTime
      || (sourceTime === selectedSourceTime && sourceCount > selectedSourceCount)
      || (
        sourceTime === selectedSourceTime
        && sourceCount === selectedSourceCount
        && selected
        && Date.parse(frame.validTime) > Date.parse(selected.validTime)
      )
    ) {
      selected = frame;
      selectedSourceTime = sourceTime;
      selectedSourceCount = sourceCount;
    }
  }
  if (selected && maxAgeMinutes !== undefined) {
    const ageMinutes = (targetTime - selectedSourceTime) / 60_000;
    if (!Number.isFinite(ageMinutes) || ageMinutes > maxAgeMinutes) return undefined;
  }
  return selected;
}

function utcClock(value: string): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "UTC",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(value));
}

function localClock(value: string): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Vancouver",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(new Date(value));
}

function shortClock(value: string): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "UTC",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(value));
}

function archiveSpan(frames: Frame[]): string {
  if (!frames.length) return "No archive coverage";
  const first = frames[0];
  const last = frames[frames.length - 1];
  if (frames.length === 1) return `1 frame · ${utcClock(first.validTime)} UTC`;
  const spanHours = Math.max(
    0,
    (Date.parse(last.validTime) - Date.parse(first.validTime)) / 3_600_000,
  );
  const span = spanHours >= 48
    ? `${(spanHours / 24).toFixed(1)} d available`
    : `${spanHours.toFixed(1)} h available`;
  return `${span} · ${utcClock(first.validTime)}–${utcClock(last.validTime)} UTC`;
}

function ageLabel(minutes: number): string {
  if (!Number.isFinite(minutes)) return "unknown";
  if (minutes < 1) return "<1m";
  return `${Math.round(minutes)}m`;
}

function layerLabel(layerId: string): string {
  if (layerId.startsWith("radar")) return "RADAR";
  if (layerId.includes("lightning")) return "LTG";
  if (layerId === "smoke") return "SMOKE";
  if (layerId === "ptype") return "PTYPE";
  if (layerId === "site-radar") return "RADAR";
  if (layerId === "hotspots") return "FIRE";
  if (
    ["daynight", "ir", "natural", "convective", "snowfog", "raw-visible", "raw-visir", "raw-ir"].includes(layerId)
    || layerId.startsWith("westwx-")
  ) return "SAT";
  return layerId.toUpperCase();
}

function sourceLabel(layerId: string): string | null {
  if (layerId.includes("coverage")) return null;
  const label = layerLabel(layerId);
  return ["SAT", "RADAR", "PTYPE", "LTG", "FIRE", "SMOKE"].includes(label) ? label : null;
}

function layerControlLabel(layerId: string): string {
  if (layerId === "natural") return "ECCC Visible";
  if (layerId === "ir") return "ECCC IR";
  if (layerId === "daynight") return "ECCC VIS/IR";
  if (layerId === "convective") return "ECCC Convective";
  if (layerId === "westwx-visible") return "NOAA Visible";
  if (layerId === "westwx-visir") return "NOAA VIS/IR";
  if (layerId === "westwx-ir") return "NOAA IR";
  if (layerId === "raw-visible") return "NOAA Visible";
  if (layerId === "raw-visir") return "NOAA VIS/IR";
  if (layerId === "raw-ir") return "NOAA IR";
  if (layerId === "radar-rain") return "Radar";
  if (layerId === "radar-snow") return "Snow rate";
  if (layerId === "radar-coverage") return "Radar coverage";
  if (layerId === "ptype-coverage") return "Precipitation-type coverage";
  if (layerId === "ptype") return "Precip type";
  if (layerId === "lightning-trail") return "Lightning";
  if (layerId === "lightning") return "Flash density";
  if (layerId === "glm-lightning-trail") return "GLM Total Lightning";
  if (layerId === "glm-lightning") return "GLM flash bins";
  if (layerId === "smoke") return "Satellite Smoke Detection";
  if (layerId === "hotspots") return "Fires & Hotspots";
  return layerLabel(layerId);
}

function legendLayerId(legendId: string): string {
  if (legendId === "lightning-age") return "lightning-trail";
  if (legendId === "glm-lightning-age") return "glm-lightning-trail";
  if (legendId === "smoke-confidence") return "smoke";
  if (legendId === "lightning-density") return "lightning";
  return legendId;
}

function freshnessThresholds(layerId: string): [number, number] {
  if (layerId.startsWith("radar") || layerId === "site-radar") return [15, 30];
  if (layerId === "ptype") return [20, 35];
  if (layerId.includes("lightning")) return [25, 45];
  if (layerId === "smoke") return [30, 60];
  if (layerId === "hotspots") return [30, 90];
  if (layerId === "raw-visible" || layerId === "raw-visir" || layerId === "raw-ir") return [90, 150];
  if (layerId.startsWith("westwx-")) return [25, 45];
  // The source valid time typically trails receipt by roughly 20–40 minutes;
  // use source-aware limits so normal ECCC publication latency is not reported
  // as a local ingest outage.
  return [45, 75];
}

function ZapIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z" />
    </svg>
  );
}

function FlameIcon({ filled = true }: { filled?: boolean }) {
  return (
    <svg viewBox="0 0 24 24" fill={filled ? "currentColor" : "none"} stroke="currentColor" strokeWidth={filled ? "2" : "2.6"} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3q1 4 4 6.5t3 5.5a1 1 0 0 1-14 0 5 5 0 0 1 1-3 1 1 0 0 0 5 0c0-2-1.5-3-1.5-5q0-2 2.5-4" />
    </svg>
  );
}

function LightningLegend() {
  const rows = [
    ["0–10 min", "age-0"],
    ["10–20 min", "age-1"],
    ["20–30 min", "age-2"],
    ["30+ min", "age-3"],
  ];
  return (
    <div className="lightning-legend" aria-label="Lightning age legend">
      {rows.map(([label, ageClass]) => (
        <div className="lightning-key-row" key={label}>
          <span className={`lightning-marker legend-marker ${ageClass}`}><ZapIcon /></span>
          <span>{label}</span>
        </div>
      ))}
    </div>
  );
}

function WatershedLegend() {
  return (
    <div className="watershed-legend" aria-label="BC Hydro watershed boundary legend">
      <span className="watershed-symbol" aria-hidden="true" />
      <span>BC Hydro watershed</span>
    </div>
  );
}

function SmokeLegend({ frame }: { frame?: Frame }) {
  return (
    <div className="hotspot-legend" aria-label="Satellite smoke detection confidence legend">
      <div className="hotspot-key-row">
        <span className="hotspot-symbol" style={{ background: "rgba(244, 220, 174, .88)" }} />
        <span>High-confidence detection</span>
      </div>
      <div className="hotspot-key-row">
        <span className="hotspot-symbol" style={{ background: "rgba(188, 204, 205, .72)" }} />
        <span>Medium-confidence detection</span>
      </div>
      <div className="hotspot-key-row">
        <span className="hotspot-symbol" style={{ background: "rgba(188, 204, 205, .72)" }} />
        <span>Low-confidence detection</span>
      </div>
      <p>
        {frame?.availability === "unavailable"
          ? "Unavailable for this scene"
          : "Daylight, sufficiently clear sky only; absence is not proof of clear air"}
      </p>
    </div>
  );
}

function FireLegend({
  hotspotFrame,
  activeFrame,
  showUsLarge,
}: {
  hotspotFrame?: Frame;
  activeFrame?: Frame;
  showUsLarge: boolean;
}) {
  const hotspotRows = [
    ["0–6 h", "age-0"],
    ["6–12 h", "age-1"],
    ["12–24 h", "age-2"],
  ];
  return (
    <div className="hotspot-legend" aria-label="Active wildfire and thermal hotspot legend">
      <div className="hotspot-key-row">
        <span className="fire-marker active-fire-marker fire-notable legend-marker"><FlameIcon /></span>
        <span>BCWS Wildfire of Note</span>
      </div>
      {showUsLarge && (
        <div className="hotspot-key-row">
          <span className="fire-marker active-fire-marker fire-notable legend-marker"><FlameIcon /></span>
          <span>U.S. current ICS-209 large incident</span>
        </div>
      )}
      <div className="hotspot-key-row">
        <span className="fire-marker active-fire-marker legend-marker"><FlameIcon /></span>
        <span>Other active wildfire</span>
      </div>
      {hotspotRows.map(([label, ageClass]) => (
        <div className="hotspot-key-row" key={label}>
          <span className={`fire-marker hotspot-fire-marker legend-marker ${ageClass}`}><FlameIcon filled={false} /></span>
          <span>Thermal hotspot · {label}</span>
        </div>
      ))}
      <p>
        {typeof activeFrame?.pointCount === "number" ? `${activeFrame.pointCount} agency-reported fires · ` : ""}
        {typeof hotspotFrame?.pointCount === "number"
          ? `${hotspotFrame.pointCount} mapped thermal detections`
          : typeof hotspotFrame?.detectionCount === "number"
            ? `${hotspotFrame.detectionCount} mapped thermal detections`
            : "Agency fires and satellite thermal detections"}
      </p>
    </div>
  );
}

function InfraredLegend() {
  const rows = [
    ["≤ −80 °C", "#ffc814"],
    ["−70 °C", "#ff5323"],
    ["−60 °C", "#dc2c74"],
    ["−50 °C", "#7e42be"],
    ["−40 °C", "#3488eb"],
    ["−30 °C", "#8ddcf9"],
    ["−20 to 20 °C", "#d0d0d0"],
  ];
  return (
    <div className="hotspot-legend" aria-label="Infrared brightness temperature legend">
      {rows.map(([label, colour]) => (
        <div className="hotspot-key-row" key={label}>
          <span className="hotspot-symbol" style={{ background: colour }} />
          <span>{label}</span>
        </div>
      ))}
      <p>Colder, taller cloud tops use stronger colours</p>
    </div>
  );
}

export function RadarViewer() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [catalogBase, setCatalogBase] = useState("");
  const [error, setError] = useState("");
  const [productId, setProductId] = useState("bc-large-overlay");
  const [frameIndex, setFrameIndex] = useState(NEWEST_FRAME);
  const [playing, setPlaying] = useState(false);
  const [speedIndex, setSpeedIndex] = useState(3);
  const [rangeHours, setRangeHours] = useState(3);
  const [optionalLayers, setOptionalLayers] = useState<Record<string, boolean>>({});
  const [freshnessClock, setFreshnessClock] = useState<number | null>(null);
  const [lightningMarkers, setLightningMarkers] = useState<LightningMarker[]>([]);
  const [ecccFallbackLightningMarkers, setEcccFallbackLightningMarkers] = useState<LightningMarker[]>([]);
  const [fireMarkers, setFireMarkers] = useState<FireMarker[]>([]);
  const preferencesRef = useRef<ViewerPreferences>({
    productId: "bc-large-overlay",
    speedIndex: 3,
    rangeHours: 3,
    optionalLayers: {},
  });

  useEffect(() => {
    let cancelled = false;
    let initialized = false;
    let loading = false;
    async function load() {
      if (loading) return;
      loading = true;
      try {
        const configResponse = await fetch("config.json", { cache: "no-store" });
        if (!configResponse.ok) throw new Error("Site configuration is unavailable.");
        const config = (await configResponse.json()) as SiteConfig;
        const candidates = [config.catalogUrl, config.fallbackCatalogUrl]
          .filter((value): value is string => Boolean(value))
          .map((value) => absoluteUrl(value, configResponse.url))
          .filter((value, index, all) => all.indexOf(value) === index);
        let lastFailure: unknown;
        for (const resolved of candidates) {
          try {
            const response = await fetch(resolved, { cache: "no-store" });
            if (!response.ok) throw new Error(`Loop catalog returned ${response.status}.`);
            const nextCatalog = (await response.json()) as Catalog;
            if (!nextCatalog.products?.length || !nextCatalog.domains) {
              throw new Error("Loop catalog is incomplete.");
            }
            const availableProducts = nextCatalog.products.filter((item) => productHasFrames(nextCatalog, item));
            if (!availableProducts.length) throw new Error("Loop catalog contains no available products.");
            if (!cancelled) {
              setCatalog(nextCatalog);
              setCatalogBase(resolved);
              let stored: Partial<ViewerPreferences> = {};
              if (!initialized) {
                try {
                  stored = JSON.parse(window.sessionStorage.getItem(VIEWER_PREFERENCES_KEY) ?? "{}") as Partial<ViewerPreferences>;
                } catch {
                  stored = {};
                }
              }
              const preferred = availableProducts.find((item) => item.id === stored.productId)
                ?? availableProducts.find((item) => item.id === "bc-large-overlay")
                ?? availableProducts[0];
              setError("");
              if (!initialized) {
                setProductId(preferred.id);
                setRangeHours(
                  typeof stored.rangeHours === "number" && RANGE_OPTIONS.includes(stored.rangeHours)
                    ? stored.rangeHours
                    : preferred.defaultHours,
                );
                setSpeedIndex(
                  typeof stored.speedIndex === "number"
                    && stored.speedIndex >= 0
                    && stored.speedIndex < PLAYBACK_SPEEDS.length
                    ? stored.speedIndex
                    : 3,
                );
                if (stored.optionalLayers && typeof stored.optionalLayers === "object") {
                  setOptionalLayers(stored.optionalLayers);
                }
                setFrameIndex(NEWEST_FRAME);
                setPlaying(!window.matchMedia("(prefers-reduced-motion: reduce)").matches);
                initialized = true;
              } else {
                setProductId((current) => (
                  availableProducts.some((item) => item.id === current) ? current : preferred.id
                ));
              }
            }
            return;
          } catch (reason) {
            lastFailure = reason;
          }
        }
        throw lastFailure ?? new Error("No loop catalog is configured.");
      } catch (reason) {
        if (!cancelled && !initialized) {
          setError(reason instanceof Error ? reason.message : "Unable to load Radar-Sat.");
        }
      } finally {
        loading = false;
      }
    }
    load();
    const interval = window.setInterval(load, 60_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    preferencesRef.current = { productId, speedIndex, rangeHours, optionalLayers };
  }, [optionalLayers, productId, rangeHours, speedIndex]);

  useEffect(() => {
    const refreshDueAt = Date.now() + AUTO_REFRESH_MS;
    const reloadIfDue = () => {
      if (Date.now() < refreshDueAt || document.visibilityState !== "visible") return;
      try {
        window.sessionStorage.setItem(
          VIEWER_PREFERENCES_KEY,
          JSON.stringify(preferencesRef.current),
        );
      } catch {
        // Storage can be disabled; refreshing remains safe without restoration.
      }
      window.location.reload();
    };
    const interval = window.setInterval(reloadIfDue, 30_000);
    document.addEventListener("visibilitychange", reloadIfDue);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", reloadIfDue);
    };
  }, []);

  useEffect(() => {
    const firstTick = window.setTimeout(() => setFreshnessClock(Date.now()), 0);
    const interval = window.setInterval(() => setFreshnessClock(Date.now()), 60_000);
    return () => {
      window.clearTimeout(firstTick);
      window.clearInterval(interval);
    };
  }, []);

  const availableProducts = useMemo(
    () => catalog?.products.filter((item) => productHasFrames(catalog, item)) ?? [],
    [catalog],
  );
  const product = useMemo(
    () => availableProducts.find((item) => item.id === productId) ?? availableProducts[0],
    [availableProducts, productId],
  );
  const domain = product ? catalog?.domains[product.domain] : undefined;
  const activeAnchorId = useMemo(
    () => product ? activeAnchorLayer(product, optionalLayers) : "",
    [optionalLayers, product],
  );
  const anchorFrames = useMemo(() => {
    if (!domain || !product) return [];
    const frames = domain.layers[activeAnchorId]?.frames ?? [];
    if (!frames.length) return [];
    const newest = Date.parse(frames[frames.length - 1].validTime);
    const cutoff = newest - rangeHours * 60 * 60 * 1000;
    return frames.filter((frame) => Date.parse(frame.validTime) >= cutoff);
  }, [activeAnchorId, domain, product, rangeHours]);

  const speed = PLAYBACK_SPEEDS[speedIndex] ?? 1;

  const currentFrameIndex = Math.min(frameIndex, Math.max(0, anchorFrames.length - 1));
  const isAnimating = playing && anchorFrames.length > 1;
  const anchor = anchorFrames[currentFrameIndex];
  const lightningController = product?.layers.find((recipe) => LIGHTNING_CONTROLLERS.has(recipe.id));
  const lightningPointsId = lightningController ? pointLayerId(lightningController.id) : undefined;
  const lightningPointReferences = useMemo(() => {
    if (
      !product
      || !domain
      || !anchor
      || !catalogBase
      || !lightningController
      || !lightningPointsId
      || !isProductLayerEnabled(lightningController, optionalLayers, product.layers)
    ) return [];
    return pointFrameReferences(
      domain.layers[lightningPointsId]?.frames ?? [],
      anchor.validTime,
      catalogBase,
      32,
      3,
    );
  }, [anchor, catalogBase, domain, lightningController, lightningPointsId, optionalLayers, product]);
  const ecccFallbackPointReferences = useMemo(() => {
    if (
      !catalog
      || !product
      || domain?.id !== "north-america"
      || !anchor
      || !catalogBase
      || !lightningController
      || !isProductLayerEnabled(lightningController, optionalLayers, product.layers)
    ) return [];
    return pointFrameReferences(
      catalog.domains.bc?.layers["lightning-points"]?.frames ?? [],
      anchor.validTime,
      catalogBase,
      32,
      3,
    );
  }, [anchor, catalog, catalogBase, domain, lightningController, optionalLayers, product]);
  const fireController = product?.layers.find((recipe) => recipe.id === "hotspots");
  const firePointReferences = useMemo(() => {
    if (
      !product
      || !domain
      || !anchor
      || !catalogBase
      || !fireController
      || !isProductLayerEnabled(fireController, optionalLayers, product.layers)
    ) return [];
    const pointDomain = domain.layers["hotspot-points"]?.frames?.length
      ? domain
      : catalog?.domains.bc;
    return rollingPointFrameReferences(
      pointDomain?.layers["hotspot-points"]?.frames ?? [],
      anchor.validTime,
      catalogBase,
      6 * 60,
    );
  }, [anchor, catalog, catalogBase, domain, fireController, optionalLayers, product]);
  const activeFirePointReferences = useMemo(() => {
    if (
      !catalog
      || !product
      || !anchor
      || !catalogBase
      || !fireController
      || !isProductLayerEnabled(fireController, optionalLayers, product.layers)
    ) return [];
    const pointDomain = domain?.layers["active-fire-points"]?.frames?.length
      ? domain
      : catalog.domains["north-america"];
    return latestRollingPointFrameReferences(
      pointDomain?.layers["active-fire-points"]?.frames ?? [],
      anchor.validTime,
      catalogBase,
      6 * 60,
    );
  }, [anchor, catalog, catalogBase, domain, fireController, optionalLayers, product]);

  const advance = useCallback(
    (amount: number) => {
      if (!anchorFrames.length) return;
      setFrameIndex((current) => {
        const safeCurrent = Math.min(Math.max(0, current), anchorFrames.length - 1);
        return (safeCurrent + amount + anchorFrames.length) % anchorFrames.length;
      });
    },
    [anchorFrames.length],
  );

  useEffect(() => {
    let cancelled = false;
    if (!lightningPointReferences.length) {
      const clearMarkers = window.setTimeout(() => setLightningMarkers([]), 0);
      return () => window.clearTimeout(clearMarkers);
    }

    void buildLightningMarkers(lightningPointReferences).then((markers) => {
      if (!cancelled) setLightningMarkers(markers);
    }).catch(() => {
      if (!cancelled) setLightningMarkers([]);
    });
    return () => { cancelled = true; };
  }, [lightningPointReferences]);

  useEffect(() => {
    let cancelled = false;
    if (!ecccFallbackPointReferences.length) {
      const clearMarkers = window.setTimeout(() => setEcccFallbackLightningMarkers([]), 0);
      return () => window.clearTimeout(clearMarkers);
    }

    void buildLightningMarkers(ecccFallbackPointReferences, "eccc-").then((markers) => {
      if (!cancelled) setEcccFallbackLightningMarkers(markers);
    }).catch(() => {
      if (!cancelled) setEcccFallbackLightningMarkers([]);
    });
    return () => { cancelled = true; };
  }, [ecccFallbackPointReferences]);

  useEffect(() => {
    let cancelled = false;
    const hotspotReference = firePointReferences[0];
    const activeReference = activeFirePointReferences[0];
    if (!hotspotReference && !activeReference) {
      const clearMarkers = window.setTimeout(() => setFireMarkers([]), 0);
      return () => window.clearTimeout(clearMarkers);
    }

    void buildFireMarkers(activeReference, hotspotReference, product?.domain ?? "bc").then((markers) => {
      if (!cancelled) setFireMarkers(markers);
    }).catch(() => {
      if (!cancelled) setFireMarkers([]);
    });
    return () => { cancelled = true; };
  }, [activeFirePointReferences, firePointReferences, product?.domain]);

  useEffect(() => {
    if (!isAnimating || !catalog || !domain || !product || !catalogBase) return;
    const nextIndex = (currentFrameIndex + 1) % anchorFrames.length;
    const nextAnchor = anchorFrames[nextIndex];
    if (!nextAnchor) return;

    // Live R2 rasters are much larger than the bundled demo. Keep displaying
    // the current map until every layer in the next composition is ready, so
    // first-pass playback can slow down but never flashes a blank frame.
    const nextUrls = composeLayers(
      product,
      domain,
      nextAnchor,
      catalogBase,
      optionalLayers,
    ).map((layer) => layer.url);
    const nextPointReferences = product.layers.flatMap((recipe) => {
      if (!isProductLayerEnabled(recipe, optionalLayers, product.layers)) return [];
      const pointsId = pointLayerId(recipe.id);
      if (!pointsId) return [];
      const pointDomain = recipe.id === "hotspots" && !domain.layers[pointsId]?.frames?.length
        ? catalog.domains.bc
        : domain;
      return recipe.id === "hotspots"
        ? rollingPointFrameReferences(
            pointDomain?.layers[pointsId]?.frames ?? [],
            nextAnchor.validTime,
            catalogBase,
            6 * 60,
          )
        : pointFrameReferences(
            pointDomain?.layers[pointsId]?.frames ?? [],
            nextAnchor.validTime,
            catalogBase,
            32,
            3,
          );
    });
    const fireRecipe = product.layers.find((recipe) => recipe.id === "hotspots");
    if (fireRecipe && isProductLayerEnabled(fireRecipe, optionalLayers, product.layers)) {
      const activePointDomain = domain.layers["active-fire-points"]?.frames?.length
        ? domain
        : catalog.domains["north-america"];
      nextPointReferences.push(...latestRollingPointFrameReferences(
        activePointDomain?.layers["active-fire-points"]?.frames ?? [],
        nextAnchor.validTime,
        catalogBase,
        6 * 60,
      ));
    }
    if (
      product.domain === "north-america"
      && product.layers.some((recipe) => (
        LIGHTNING_CONTROLLERS.has(recipe.id)
        && isProductLayerEnabled(recipe, optionalLayers, product.layers)
      ))
    ) {
      nextPointReferences.push(...pointFrameReferences(
        catalog.domains.bc?.layers["lightning-points"]?.frames ?? [],
        nextAnchor.validTime,
        catalogBase,
        32,
        3,
      ));
    }
    nextPointReferences.forEach((reference) => preloadPointFrame(reference.url));
    let cancelled = false;
    let timer: number | undefined;
    const loads = nextUrls.map((url) => new Promise<void>((resolve) => {
      const image = new Image();
      image.onload = () => resolve();
      image.onerror = () => resolve();
      image.src = url;
      if (image.complete) resolve();
    }));
    loads.push(...nextPointReferences.map((reference) => (
      loadPointFrame(reference.url).then(() => undefined).catch(() => undefined)
    )));

    void Promise.all(loads).then(() => {
      if (cancelled) return;
      const finalFrame = currentFrameIndex === anchorFrames.length - 1;
      // The former 4× timing is now the easier-to-understand 1× baseline.
      const delay = finalFrame ? 300 / speed : 75 / speed;
      timer = window.setTimeout(() => advance(1), delay);
    });

    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [
    advance,
    anchorFrames,
    catalog,
    catalogBase,
    currentFrameIndex,
    domain,
    isAnimating,
    optionalLayers,
    product,
    speed,
  ]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        setPlaying(false);
        advance(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        setPlaying(false);
        advance(1);
      } else if (event.key === " ") {
        event.preventDefault();
        setPlaying((value) => !value);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [advance]);

  if (error) {
    return (
      <main className="app-shell">
        <h1 className="brand">BC Satellite/Radar/Lightning</h1>
        <div className="error-panel" role="alert">{error}</div>
      </main>
    );
  }
  if (!catalog || !product || !domain) return <main className="loading-page">Loading observational loops…</main>;

  const isLayerEnabled = (recipe: ProductLayer) =>
    isProductLayerEnabled(recipe, optionalLayers, product.layers);
  const composedLayers = anchor
    ? composeLayers(product, domain, anchor, catalogBase, optionalLayers)
    : [];
  const pointSourceTimes = [
    lightningPointReferences.length || ecccFallbackPointReferences.length
      ? {
          label: "LTG",
          validTime: [
            lightningPointReferences[lightningPointReferences.length - 1]?.validTime,
            ecccFallbackPointReferences[ecccFallbackPointReferences.length - 1]?.validTime,
          ]
            .filter((value): value is string => Boolean(value))
            .sort((left, right) => Date.parse(right) - Date.parse(left))[0],
        }
      : undefined,
    firePointReferences[0] || activeFirePointReferences[0]
      ? {
          label: "FIRE",
          validTime: [firePointReferences[0]?.validTime, activeFirePointReferences[0]?.validTime]
            .filter((value): value is string => Boolean(value))
            .sort((left, right) => Date.parse(right) - Date.parse(left))[0],
        }
      : undefined,
  ].filter((item): item is { label: string; validTime: string } => Boolean(item));
  const sourceTimes = composedLayers
    .filter((item): item is typeof item & { frame: Frame } => "frame" in item && Boolean(item.frame))
    .map((item) => ({ label: sourceLabel(item.id), validTime: actualSourceTime(item.id, item.frame) }))
    .filter((item): item is { label: string; validTime: string } => Boolean(item.label))
    .concat(pointSourceTimes)
    .filter((item, index, all) => all.findIndex((candidate) => candidate.label === item.label && candidate.validTime === item.validTime) === index)
    .map((item) => `${item.label} ${shortClock(item.validTime)}`)
    .join(" · ");
  const composedLayerIds = new Set(composedLayers.map((layer) => layer.id));
  if (lightningController && (lightningPointReferences.length || ecccFallbackPointReferences.length)) {
    composedLayerIds.add(lightningController.id);
  }
  if (fireController && (firePointReferences.length || activeFirePointReferences.length)) {
    composedLayerIds.add(fireController.id);
  }
  const missingLayers = product.layers
    .filter((recipe) => isLayerEnabled(recipe) && !domain.staticLayers[recipe.id] && !composedLayerIds.has(recipe.id))
    .map((recipe) => layerControlLabel(recipe.id))
    .filter((label, index, all) => all.indexOf(label) === index);
  const hasCoverage = composedLayers.some((layer) => layer.id.includes("coverage"));
  const optional = product.layers.filter((layer) => layer.optional);
  const visibleLegends = product.legends.filter((legendId) => {
    const recipe = product.layers.find((layer) => layer.id === legendLayerId(legendId));
    return !recipe || isLayerEnabled(recipe);
  });
  const activeSourceFreshness = product.layers
    .filter(isLayerEnabled)
    .flatMap((recipe) => {
      const frames = domain.layers[recipe.id]?.frames ?? [];
      const frame = frames[frames.length - 1];
      const label = sourceLabel(recipe.id);
      if (!frame || !label || freshnessClock === null) return [];
      const age = Math.max(0, (freshnessClock - Date.parse(actualSourceTime(recipe.id, frame))) / 60_000);
      const [currentLimit, delayedLimit] = freshnessThresholds(recipe.id);
      return [{ label, age, currentLimit, delayedLimit }];
    })
    .filter((item, index, all) => all.findIndex((candidate) => candidate.label === item.label) === index);
  const allSourcesCurrent = activeSourceFreshness.length > 0
    && activeSourceFreshness.every((item) => item.age <= item.currentLimit);
  const anySourceUsable = activeSourceFreshness.some((item) => item.age <= item.delayedLimit);
  const liveState = freshnessClock === null
    ? "Checking"
    : allSourcesCurrent
      ? "Current"
      : anySourceUsable
        ? "Delayed"
      : "Archive";
  const freshnessDetails = activeSourceFreshness
    .map((item) => `${item.label} ${ageLabel(item.age)}`)
    .join(" · ");
  const liveSummaryLabel = freshnessClock === null
    ? "Checking data freshness"
    : liveState === "Current"
      ? `Current${freshnessDetails ? ` · ${freshnessDetails}` : ""}`
      : liveState === "Delayed"
        ? `Mixed freshness${freshnessDetails ? ` · ${freshnessDetails}` : ""}`
        : `Not live${freshnessDetails ? ` · ${freshnessDetails}` : ""}`;
  const selectedArchiveSpan = archiveSpan(anchorFrames);
  const viewport = product.viewport ?? FULL_VIEWPORT;
  const mapAspect = (domain.width * viewport.width) / (domain.height * viewport.height);
  const cropStyle: CSSProperties = {
    left: `${-(viewport.left / viewport.width) * 100}%`,
    top: `${-(viewport.top / viewport.height) * 100}%`,
    right: "auto",
    bottom: "auto",
    width: `${100 / viewport.width}%`,
    height: `${100 / viewport.height}%`,
  };

  return (
    <main className="app-shell">
      <header className="site-header">
        <div className="brand-row">
          <h1 className="brand">BC Satellite<span className="brand-mark">/</span>Radar<span className="brand-mark">/</span>Lightning</h1>
        </div>
        <div className="live-summary" aria-live="polite">
          <span className={`status-dot status-${liveState.toLowerCase()}`} aria-hidden="true" />
          <span>{liveSummaryLabel} · catalog {utcClock(catalog.generatedAt)} UTC</span>
        </div>
      </header>

      <nav className="product-nav" aria-label="Loop products">
        {availableProducts.map((item) => (
          <button
            className="product-button"
            type="button"
            aria-pressed={item.id === product.id}
            key={item.id}
            onClick={() => {
              setPlaying(true);
              setProductId(item.id);
              setRangeHours(item.defaultHours);
              setFrameIndex(NEWEST_FRAME);
            }}
          >
            {item.shortTitle}
          </button>
        ))}
      </nav>

      <section className="viewer-grid" aria-label={product.title}>
        <div
          className="map-column"
          style={{
            "--map-aspect": `${mapAspect}`,
          } as CSSProperties}
        >
          <div className="timeline-panel">
            <div className="transport-row">
              <div className="transport-actions">
                <button className="control-button" type="button" aria-label="Previous frame" disabled={anchorFrames.length < 2} onClick={() => { setPlaying(false); advance(-1); }}>‹</button>
                <button className="control-button primary" type="button" aria-pressed={isAnimating} disabled={anchorFrames.length < 2} onClick={() => setPlaying((value) => !value)}>{isAnimating ? "Pause" : "Play"}</button>
                <button className="control-button" type="button" aria-label="Next frame" disabled={anchorFrames.length < 2} onClick={() => { setPlaying(false); advance(1); }}>›</button>
              </div>
              <label className="speed-control">
                <span>Speed</span>
                <input
                  className="speed-range"
                  type="range"
                  min={0}
                  max={PLAYBACK_SPEEDS.length - 1}
                  step={1}
                  value={speedIndex}
                  aria-valuetext={`${speed} times`}
                  onChange={(event) => setSpeedIndex(Number(event.target.value))}
                />
                <span className="speed-value">{speed}×</span>
              </label>
              <div className="timeline-metadata">
                <span className="frame-count">{anchorFrames.length ? `${currentFrameIndex + 1} / ${anchorFrames.length}` : "0 / 0"}</span>
                <span className="archive-span">{selectedArchiveSpan}</span>
              </div>
            </div>
            <div className="range-row">
              <div className="range-actions" role="group" aria-label="Archive range">
                {RANGE_OPTIONS.map((hours) => (
                  <button className="range-button" type="button" aria-pressed={rangeHours === hours} key={hours} onClick={() => { setRangeHours(hours); setFrameIndex(NEWEST_FRAME); setPlaying(true); }}>
                    {hours === 168 ? "7 d" : `${hours} h`}
                  </button>
                ))}
              </div>
              {optional.length > 0 && (
                <div className="layer-actions" role="group" aria-label="Overlay layers">
                  {optional.map((layer) => (
                    <label className="field-select" key={layer.id}>
                      <input
                        type="checkbox"
                        checked={isLayerEnabled(layer)}
                        onChange={(event) => {
                          const checked = event.target.checked;
                          setOptionalLayers((current) => {
                            const next = { ...current };
                            if (checked && layer.choiceGroup) {
                              for (const peer of optional) {
                                if (peer.choiceGroup === layer.choiceGroup) next[peer.id] = false;
                              }
                            }
                            next[layer.id] = checked;
                            return next;
                          });
                          setFrameIndex(NEWEST_FRAME);
                          setPlaying(true);
                        }}
                      />
                      {layerControlLabel(layer.id)}
                    </label>
                  ))}
                </div>
              )}
            </div>
            <input
              className="timeline-range"
              aria-label="Loop frame"
              type="range"
              min={0}
              max={Math.max(0, anchorFrames.length - 1)}
              value={currentFrameIndex}
              disabled={anchorFrames.length < 2}
              onChange={(event) => { setPlaying(false); setFrameIndex(Number(event.target.value)); }}
            />
          </div>

          <div
            className="map-stage"
            role="img"
            aria-label={`${product.title}${anchor ? `, valid ${utcClock(anchor.validTime)} UTC. ${sourceTimes}` : ", no frames available"}`}
          >
            {!anchor && <div className="map-loading">No frames are available for this product yet.</div>}
            {composedLayers.map((layer) => (
              // Raw overlay rasters must retain their exact common-grid dimensions.
              // eslint-disable-next-line @next/next/no-img-element
              <img
                className="map-layer"
                src={layer.url}
                alt=""
                aria-hidden="true"
                key={`${layer.id}-${layer.url}`}
                style={{
                  ...cropStyle,
                  opacity: layer.opacity,
                  filter: ["Overlay", "Broad"].includes(product.group)
                    && ["natural", "ir", "daynight", "convective", "raw-visible", "raw-visir", "raw-ir", "westwx-visible", "westwx-visir", "westwx-ir"].includes(layer.id)
                    && (composedLayerIds.has("radar-rain") || composedLayerIds.has("ptype"))
                    ? "saturate(0.52) brightness(0.78) contrast(1.06)"
                    : undefined,
                }}
              />
            ))}
            {lightningMarkers.length > 0 && (
              <div
                className="point-symbol-layer"
                style={cropStyle}
                role="img"
                aria-label="Recent lightning activity; brighter bolts are newer"
              >
                {lightningMarkers.map((event) => (
                  <span
                    className={`lightning-marker age-${event.age}`}
                    key={event.id}
                    style={{ left: `${event.x}%`, top: `${event.y}%` }}
                  >
                    <ZapIcon />
                  </span>
                ))}
              </div>
            )}
            {ecccFallbackLightningMarkers.length > 0 && product.domain === "north-america" && (
              <div
                className="point-symbol-layer eccc-north-fallback"
                style={BC_ON_NORTH_AMERICA_STYLE}
                role="img"
                aria-label="Recent ECCC lightning activity in northern British Columbia"
              >
                {ecccFallbackLightningMarkers.map((event) => (
                  <span
                    className={`lightning-marker age-${event.age}`}
                    key={event.id}
                    style={{ left: `${event.x}%`, top: `${event.y}%` }}
                  >
                    <ZapIcon />
                  </span>
                ))}
              </div>
            )}
            {fireMarkers.length > 0 && (
              <div
                className="point-symbol-layer"
                style={cropStyle}
                role="img"
                aria-label="Agency-reported active wildfires and satellite thermal hotspots"
              >
                {fireMarkers.map((marker) => (
                  <span
                    className={`fire-marker ${marker.kind === "active" ? "active-fire-marker" : "hotspot-fire-marker"}${marker.notable ? " fire-notable" : ""} age-${marker.age}`}
                    key={marker.id}
                    style={{ left: `${marker.x}%`, top: `${marker.y}%` }}
                    title={marker.highlight === 1
                      ? marker.count > 1
                        ? `${marker.count} nearby BCWS Wildfires of Note`
                        : "BCWS Wildfire of Note"
                      : marker.highlight === 2
                        ? "U.S. current ICS-209 large incident"
                        : marker.kind === "active"
                          ? "Agency-reported active wildfire"
                          : "Satellite thermal hotspot"}
                  >
                    <FlameIcon filled={marker.kind === "active"} />
                    {marker.count > 1 && <span className="fire-count">{marker.count}</span>}
                  </span>
                ))}
              </div>
            )}
            {anchor && (
              <div className="map-status">
                <p className="valid-line">VALID {utcClock(anchor.validTime)} UTC · {localClock(anchor.validTime)}</p>
                <p className="source-times">{sourceTimes || `SOURCE ${shortClock(anchor.validTime)}`}</p>
                {missingLayers.length > 0 && (
                  <p className="source-warning">Unavailable: {missingLayers.join(", ")}</p>
                )}
              </div>
            )}
            {hasCoverage && (
              <div className="coverage-key"><span className="hatch-swatch" /> No radar coverage</div>
            )}
          </div>
        </div>

        <aside className="legend-rail" aria-label="Map legends">
          <h2 className="legend-title">Legend</h2>
          {visibleLegends.map((legendId) => {
            const legend = catalog.legends[legendId];
            if (!legend) return null;
            if (legend.kind === "lightning-age") return <LightningLegend key={legendId} />;
            if (legend.kind === "smoke-confidence") {
              const smokeFrame = composedLayers.find((layer) => layer.id === "smoke")?.frame;
              return <SmokeLegend frame={smokeFrame} key={legendId} />;
            }
            if (legend.kind === "hotspots") {
              const hotspotFrame = firePointReferences[0]?.frame
                ?? composedLayers.find((layer) => layer.id === "hotspots")?.frame;
              return (
                <FireLegend
                  hotspotFrame={hotspotFrame}
                  activeFrame={activeFirePointReferences[0]?.frame}
                  showUsLarge={product.domain === "north-america"}
                  key={legendId}
                />
              );
            }
            if (legend.kind === "raw-ir") return <InfraredLegend key={legendId} />;
            if (legend.kind === "watersheds") return <WatershedLegend key={legendId} />;
            return legend.path ? (
              // Legend rasters are supplied by the authoritative data source.
              // eslint-disable-next-line @next/next/no-img-element
              <img className="legend-image" src={absoluteUrl(legend.path, catalogBase)} alt={legend.title} key={legendId} />
            ) : null;
          })}
          {!visibleLegends.length && (
            <p className="detail-copy">
              {product.id === "bc-ir"
                ? "Enhanced RGB; no calibrated brightness-temperature scale."
                : product.group === "Satellite"
                  ? "Qualitative satellite RGB; no calibrated numerical scale."
                  : product.legends.length
                    ? "No legend-bearing layers are enabled."
                    : "Qualitative RGB product; no numerical scale."}
            </p>
          )}
        </aside>
      </section>

      <section className="product-detail">
        <div>
          <h2 className="detail-title">{product.title}</h2>
          <p className="detail-copy">{product.description}</p>
        </div>
        <ul className="note-list">
          {product.notes.map((note) => <li key={note}>{note}</li>)}
        </ul>
      </section>
    </main>
  );
}
