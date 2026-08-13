"use client";

import { CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { loadPointFrame, preloadPointFrame } from "./point-data";
import {
  VideoCanvasProxyLayer,
  VideoCompositeFramePlan,
  VideoCompositeStage,
} from "./video-composite-stage";
import {
  loadVideoLoopManifest,
  selectVideoFrames,
  sourceCacheKey,
  supportsVideoLoop,
  VideoLoopManifest,
  VideoProfilePointer,
} from "./video-loop";

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
  usFeatureCount?: number;
  sourceErrors?: string[];
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
  staticLayers: Record<string, { path: string; revision?: string }>;
};

type ProductLayer = {
  id: string;
  opacity: number;
  optional?: boolean;
  defaultEnabled?: boolean;
  choiceGroup?: string;
  enabledWith?: string;
  controlId?: string;
  controlSection?: string;
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
  frameIntervalMinutes?: number;
  archiveFrameIntervalMinutes?: number;
  defaultHours: number;
  maxHours?: number;
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
  catalogMode?: "index" | "full";
  fullCatalogPath?: string;
  domains: Record<string, Domain>;
  products: Product[];
  legends: Record<string, Legend>;
  sources?: Record<string, string>;
  videoProfiles?: Record<string, Record<string, {
    live?: VideoProfilePointer;
    archive?: VideoProfilePointer;
  }>>;
};

type SiteConfig = {
  catalogUrl: string;
  catalogIndexUrl?: string;
  fallbackCatalogUrl?: string;
};

const RANGE_OPTIONS = [3, 6, 12, 24, 168];
const PLAYBACK_SPEEDS = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4];
const VIDEO_HUD_UPDATE_INTERVAL_MS = 180;
const VIEWER_PREFERENCES_KEY = "radar-sat-viewer-preferences-v5";
const LEGACY_VIEWER_PREFERENCES_KEY = "radar-sat-viewer-preferences-v4";
const NEWEST_FRAME = Number.MAX_SAFE_INTEGER;
const FULL_VIEWPORT: Viewport = { left: 0, top: 0, width: 1, height: 1 };
const FULL_LAYER_STYLE: CSSProperties = {
  left: "0",
  top: "0",
  right: "auto",
  bottom: "auto",
  width: "100%",
  height: "100%",
};
const REGIONAL_PRODUCT_KEYS: Record<string, string> = {
  "bc-small-overlay": "small",
  "bc-southwest-overlay": "southwest",
  "bc-southeast-overlay": "southeast",
  "bc-northeast-overlay": "northeast",
};
const BC_ON_NORTH_AMERICA = { left: 0.269, top: 0.258, width: 0.286, height: 0.333 };
const BC_ON_NORTH_AMERICA_STYLE: CSSProperties = {
  left: `${BC_ON_NORTH_AMERICA.left * 100}%`,
  top: `${BC_ON_NORTH_AMERICA.top * 100}%`,
  width: `${BC_ON_NORTH_AMERICA.width * 100}%`,
  height: `${BC_ON_NORTH_AMERICA.height * 100}%`,
};
const LIGHTNING_CONTROLLERS = new Set(["lightning-trail", "glm-lightning-trail"]);
// The derived lightning trails are compact transparent PNGs. Prefer them to
// downloading point JSON and repainting hundreds of symbols in the browser on
// every animation frame, but still decode them before advancing the clock.
const RASTER_LIGHTNING_OVERLAYS = true;
// Active-fire and hotspot point payloads remain available for crisp regional
// rendering, but overview loops use a precomposed transparent PNG to avoid
// repainting hundreds of flame paths on every animation frame.
const RASTER_FIRE_OVERLAYS = true;
const WEB_MERCATOR_RADIUS = 6_378_137;
const WGS84_ECCENTRICITY = 0.08181919084262149;
const NORTH_AMERICA_BOUNDS = [-21_051_700.011, 557_305.257, -4_551_782.871, 12_932_243.112] as const;
const NORTH_PACIFIC_BOUNDS = [-3_339_584.7, 764_000, 15_584_728.7, 11_413_000] as const;
type ImageFrameCacheEntry = {
  image: HTMLImageElement;
  promise: Promise<boolean>;
};

const imageFrameCache = new Map<string, ImageFrameCacheEntry>();
const lightningMarkerCache = new Map<string, Promise<LightningMarker[]>>();
const fireMarkerCache = new Map<string, Promise<FireMarker[]>>();
const IMAGE_FRAME_CACHE_LIMIT = 16;
const MARKER_CACHE_LIMIT = 96;
const IMAGE_LOAD_TIMEOUT_MS = 10_000;
const PLAYBACK_IMAGE_RETRIES = 2;
const PLAYBACK_IMAGE_RETRY_DELAY_MS = 240;
const SOURCE_SUMMARIES: Record<string, string> = {
  "NOAA GOES-18": "Calibrated ABI satellite imagery, GLM total lightning and smoke-detection products.",
  "NOAA Open Data": "Public cloud distribution for GOES ABI Level-2 satellite source files.",
  "NOAA/NESDIS/STAR": "Full-resolution CIRA GeoColor imagery distributed by NOAA/NESDIS/STAR; the raw NOAA feed remains the automatic fallback.",
  "ECCC GeoMet": "Canadian radar, precipitation type and ECCC-rendered satellite products.",
  "ECCC Datamart": "Native MSC GOES RGB satellite products and Canadian Lightning Detection Network observations.",
  "ECCC HRDPS Continental 2.5 km": "Hourly 500 hPa geopotential-height and mean sea-level-pressure model contours.",
  "ECMWF IFS Control": "Contours linearly interpolated from three-hour control-forecast fields: hourly for 24 hours, then three-hourly through day seven on the large domains.",
  "NRCan CWFIS": "Timestamped satellite thermal detections and Canadian active-fire records.",
  "BC Wildfire Service": "Official BC active fires and Wildfires of Note.",
  "NIFC WFIGS": "Current U.S. ICS-209 large-incident locations.",
  "GeoBC": "Public BC transmission-line geometry used in the forecast fire-weather maps.",
};

function pointLayerId(controllerId: string): string | undefined {
  if (controllerId === "lightning-trail") return "lightning-points";
  if (controllerId === "glm-lightning-trail") return "glm-lightning-points";
  if (controllerId === "hotspots") return "hotspot-points";
  return undefined;
}

function usesRasterLightning(product: Product | undefined): boolean {
  return RASTER_LIGHTNING_OVERLAYS && Boolean(product);
}

function usesRasterFire(product: Product | undefined): boolean {
  return RASTER_FIRE_OVERLAYS && Boolean(product);
}

function absoluteUrl(path: string, base: string): string {
  return new URL(path, base).toString();
}

function productHasFrames(catalog: Catalog, product: Product): boolean {
  const domain = catalog.domains[product.domain];
  if (product.anchorLayer === "raw-visir-5min") {
    return Boolean(
      domain?.layers["raw-visir-5min"]?.frames?.length
      || domain?.layers["raw-visir"]?.frames?.length,
    );
  }
  return Boolean(domain?.layers[product.anchorLayer]?.frames?.length);
}

function mergedFrames(...collections: Frame[][]): Frame[] {
  const byTime = new Map<number, Frame>();
  for (const frames of collections) {
    for (const frame of frames) {
      const validTime = Date.parse(frame.validTime);
      if (Number.isFinite(validTime)) byTime.set(Math.round(validTime / 300_000), frame);
    }
  }
  return [...byTime.values()].sort((left, right) => (
    Date.parse(left.validTime) - Date.parse(right.validTime)
  ));
}

function playbackFrames(
  sourceFrames: Frame[],
  rangeHours: number,
  frameIntervalMinutes = 5,
  archiveFrameIntervalMinutes = 30,
): Frame[] {
  if (!sourceFrames.length) return [];
  const selectedIntervalMinutes = rangeHours > 24
    ? Math.max(frameIntervalMinutes, archiveFrameIntervalMinutes)
    : Math.max(5, frameIntervalMinutes);
  const frameIntervalMs = selectedIntervalMinutes * 60_000;
  const parsed = sourceFrames
    .map((frame) => ({ frame, time: Date.parse(frame.validTime) }))
    .filter((item) => Number.isFinite(item.time))
    .sort((left, right) => left.time - right.time);
  if (!parsed.length) return [];
  const newest = Math.floor(parsed[parsed.length - 1].time / frameIntervalMs) * frameIntervalMs;
  const requestedStart = newest - rangeHours * 60 * 60_000;
  const availableStart = Math.ceil(parsed[0].time / frameIntervalMs) * frameIntervalMs;
  const start = Math.max(requestedStart, availableStart);
  const times: number[] = [];
  let current = Math.ceil(start / frameIntervalMs) * frameIntervalMs;
  while (current <= newest) {
    times.push(current);
    current += frameIntervalMs;
  }
  let sourceIndex = 0;
  return times.flatMap((time): Frame[] => {
    while (
      sourceIndex + 1 < parsed.length
      && parsed[sourceIndex + 1].time <= time
    ) {
      sourceIndex += 1;
    }
    const source = parsed[sourceIndex];
    if (source.time > time) return [];
    return [{
      ...source.frame,
      validTime: new Date(time).toISOString(),
    }];
  });
}

function nearestFrame(frames: Frame[], target: string, toleranceMinutes: number): Frame | undefined {
  const targetTime = Date.parse(target);
  if (!Number.isFinite(targetTime)) return undefined;
  let selected: Frame | undefined;
  let selectedOffset = Number.POSITIVE_INFINITY;
  for (const frame of frames) {
    const offset = Math.abs(Date.parse(frame.validTime) - targetTime);
    if (Number.isFinite(offset) && offset < selectedOffset) {
      selected = frame;
      selectedOffset = offset;
    }
  }
  return selectedOffset <= toleranceMinutes * 60_000 ? selected : undefined;
}

function frameUrl(frame: Frame, base: string): string {
  const url = new URL(frame.path, base);
  // A same-valid-time source can be corrected after first publication. The
  // fetch timestamp makes that replacement visible to a long-open browser.
  url.searchParams.set("v", frame.fetchedAt);
  return url.toString();
}

function preloadImageFrame(
  url: string,
  priority: "high" | "low" | "auto" = "auto",
): Promise<boolean> {
  const existing = imageFrameCache.get(url);
  if (existing) {
    imageFrameCache.delete(url);
    imageFrameCache.set(url, existing);
    return existing.promise;
  }
  const image = new Image();
  const request = new Promise<boolean>((resolve) => {
    let finished = false;
    const finish = (loaded: boolean) => {
      if (finished) return;
      finished = true;
      window.clearTimeout(timeout);
      image.onload = null;
      image.onerror = null;
      if (loaded && typeof image.decode === "function") {
        void image.decode()
          .then(() => resolve(true))
          .catch(() => resolve(image.naturalWidth > 0));
      } else {
        resolve(loaded);
      }
    };
    const timeout = window.setTimeout(() => finish(false), IMAGE_LOAD_TIMEOUT_MS);
    image.decoding = "async";
    image.fetchPriority = priority;
    image.onload = () => finish(true);
    image.onerror = () => finish(false);
    image.src = url;
    if (image.complete) finish(image.naturalWidth > 0);
  });
  const entry = { image, promise: request };
  imageFrameCache.set(url, entry);
  // A transient R2/network failure must not poison this URL for the rest of
  // the browser session. Removing failed promises lets the playback gate
  // retry them on the next pass instead of leaving the raster permanently
  // stuck while lightweight lightning overlays continue to advance.
  void request.then((loaded) => {
    if (!loaded && imageFrameCache.get(url) === entry) {
      imageFrameCache.delete(url);
    }
  });
  while (imageFrameCache.size > IMAGE_FRAME_CACHE_LIMIT) {
    const oldest = imageFrameCache.keys().next().value;
    if (typeof oldest !== "string") break;
    imageFrameCache.delete(oldest);
  }
  return request;
}

function releasePreloadedImage(url: string): void {
  imageFrameCache.delete(url);
}

function StableMapImage({
  src,
  className,
  style,
  layerId,
}: {
  src: string;
  className: string;
  style: CSSProperties;
  layerId: string;
}) {
  const [slots, setSlots] = useState<[string, string]>([src, src]);
  const [activeSlot, setActiveSlot] = useState(0);
  const activeSlotRef = useRef(0);
  const requestedSrcRef = useRef(src);

  useEffect(() => {
    requestedSrcRef.current = src;
    if (slots[activeSlotRef.current] === src) return;
    const loadingSlot = activeSlotRef.current === 0 ? 1 : 0;
    setSlots((current) => {
      if (current[loadingSlot] === src) return current;
      const next: [string, string] = [...current];
      next[loadingSlot] = src;
      return next;
    });
  }, [slots, src]);

  const revealLoadedSlot = (slotIndex: number, loadedSrc: string) => {
    if (requestedSrcRef.current !== loadedSrc) return;
    activeSlotRef.current = slotIndex;
    setActiveSlot(slotIndex);
    // The persistent DOM buffer now owns this decoded surface. Keeping the
    // temporary preload object as well would let a long-running loop retain
    // many full-resolution satellite/radar frames and invite memory pressure.
    releasePreloadedImage(loadedSrc);
  };

  // Two persistent DOM images make the swap atomic: the decoded front raster
  // stays painted while the back raster loads, then their opacity flips only
  // after the back image's own load event. A separate preload alone is not
  // sufficient on memory-constrained browsers, which may evict its decoded
  // surface before React assigns the same URL to the visible image.
  return (
    <>
      {slots.map((slotSrc, slotIndex) => (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          className={className}
          src={slotSrc}
          alt=""
          aria-hidden="true"
          data-buffer-state={slotIndex === activeSlot ? "active" : "standby"}
          data-layer-id={slotIndex === activeSlot ? layerId : `${layerId}-standby`}
          key={slotIndex}
          onLoad={() => revealLoadedSlot(slotIndex, slotSrc)}
          style={{
            ...style,
            opacity: slotIndex === activeSlot ? style.opacity : 0,
            pointerEvents: "none",
          }}
        />
      ))}
    </>
  );
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
  // Never put a future fire snapshot on an earlier weather frame. If the
  // archive does not yet contain an at-or-before observation, omit the fire
  // overlay instead of labelling a future valid time.
  return pointFrameReferences(frames, target, catalogBase, maxAgeMinutes, 1);
}

function resilientActiveFireFrameReferences(
  frames: Frame[],
  target: string,
  catalogBase: string,
  maxAgeMinutes: number,
): PointFrameReference[] {
  const current = rollingPointFrameReferences(frames, target, catalogBase, maxAgeMinutes);
  const selected = current[0];
  const nifcFailed = selected?.frame.sourceErrors?.some((error) => error.includes("NIFC WFIGS"));
  if (!selected || !nifcFailed) return current;
  const targetTime = Date.parse(target);
  const fallback = [...frames].reverse().find((frame) => {
    const validTime = Date.parse(frame.validTime);
    return Number.isFinite(validTime)
      && validTime <= targetTime
      && targetTime - validTime <= maxAgeMinutes * 60_000
      && (frame.usFeatureCount ?? 0) > 0
      && !(frame.sourceErrors?.length);
  });
  if (!fallback) return current;
  return [{
    validTime: fallback.validTime,
    url: frameUrl(fallback, catalogBase),
    ageMinutes: Math.max(0, (targetTime - Date.parse(fallback.validTime)) / 60_000),
    frame: fallback,
  }];
}

async function buildLightningMarkers(
  references: PointFrameReference[],
  idPrefix = "",
  maxMarkers = 1_200,
  targetDomain?: string,
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
      const mapped = remapFirePoint(x, y, payload.domain, targetDomain ?? payload.domain);
      if (!mapped) return;
      const totalAge = Math.max(0, reference.ageMinutes + pointAge);
      const age: LightningMarker["age"] = totalAge < 10 ? 0 : totalAge < 20 ? 1 : totalAge < 30 ? 2 : 3;
      const location = `${Math.round(x * 10_000)}-${Math.round(y * 10_000)}`;
      const marker = {
        id: `${idPrefix}${payload.domain}-${reference.validTime}-${index}`,
        x: mapped[0] * 100,
        y: mapped[1] * 100,
        age,
      } satisfies LightningMarker;
      const previous = byLocation.get(location);
      if (!previous || marker.age < previous.age) byLocation.set(location, marker);
    });
  }
  const freshest = [...byLocation.values()]
    .sort((left, right) => left.age - right.age)
    .slice(0, maxMarkers);
  // Paint older symbols first so the bright newest detections remain legible.
  return freshest.sort((left, right) => right.age - left.age);
}

function cachedLightningMarkers(
  references: PointFrameReference[],
  idPrefix: string,
  maxMarkers: number,
  targetDomain: string | undefined,
): Promise<LightningMarker[]> {
  const key = [
    targetDomain ?? "",
    idPrefix,
    maxMarkers,
    ...references.map((reference) => `${reference.url}@${reference.ageMinutes.toFixed(2)}`),
  ].join("|");
  const existing = lightningMarkerCache.get(key);
  if (existing) {
    lightningMarkerCache.delete(key);
    lightningMarkerCache.set(key, existing);
    return existing;
  }
  const request = buildLightningMarkers(references, idPrefix, maxMarkers, targetDomain)
    .catch((error) => {
      lightningMarkerCache.delete(key);
      throw error;
    });
  lightningMarkerCache.set(key, request);
  while (lightningMarkerCache.size > MARKER_CACHE_LIMIT) {
    const oldest = lightningMarkerCache.keys().next().value;
    if (typeof oldest !== "string") break;
    lightningMarkerCache.delete(oldest);
  }
  return request;
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
  if (sourceDomain === "north-america" && targetDomain === "north-pacific") {
    const [naXmin, naYmin, naXmax, naYmax] = NORTH_AMERICA_BOUNDS;
    const projectedX = naXmin + x * (naXmax - naXmin);
    const projectedY = naYmax - y * (naYmax - naYmin);
    const longitude = projectedX / WEB_MERCATOR_RADIUS * 180 / Math.PI;
    const latitude = Math.atan(Math.sinh(projectedY / WEB_MERCATOR_RADIUS));
    const wrappedLongitude = longitude < 0 ? longitude + 360 : longitude;
    const pacificX = WEB_MERCATOR_RADIUS * (wrappedLongitude - 150) * Math.PI / 180;
    const sineLatitude = Math.sin(latitude);
    const pacificY = WEB_MERCATOR_RADIUS * Math.log(
      Math.tan(Math.PI / 4 + latitude / 2)
      * ((1 - WGS84_ECCENTRICITY * sineLatitude) / (1 + WGS84_ECCENTRICITY * sineLatitude)) ** (WGS84_ECCENTRICITY / 2),
    );
    const [npXmin, npYmin, npXmax, npYmax] = NORTH_PACIFIC_BOUNDS;
    const mappedX = (pacificX - npXmin) / (npXmax - npXmin);
    const mappedY = (npYmax - pacificY) / (npYmax - npYmin);
    if (mappedX < 0 || mappedX > 1 || mappedY < 0 || mappedY > 1) return undefined;
    return [mappedX, mappedY];
  }
  return undefined;
}

function clusterNotableFires(markers: FireMarker[], targetDomain: string): FireMarker[] {
  const regular = markers.filter((marker) => !marker.notable);
  const remaining = markers.filter((marker) => marker.notable);
  const clustered: FireMarker[] = [];
  const clusterDistance = targetDomain === "bc" ? 0.8 : 0.35;
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
  // Both continental domains are overview maps. Filtering weak hotspots and
  // routine agency points avoids redrawing nearly two thousand sub-pixel
  // symbols per Pacific frame while retaining notable fires and strong heat.
  const overview = targetDomain === "north-america" || targetDomain === "north-pacific";
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

function cachedFireMarkers(
  activeReference: PointFrameReference | undefined,
  hotspotReference: PointFrameReference | undefined,
  targetDomain: string,
): Promise<FireMarker[]> {
  // Fire display age changes only at 6/12-hour boundaries. A 30-minute age
  // bucket avoids rebuilding hundreds of markers on every 5/10-minute map.
  const hotspotAgeBucket = Math.floor((hotspotReference?.ageMinutes ?? 0) / 30);
  const key = [
    targetDomain,
    activeReference?.url ?? "",
    hotspotReference?.url ?? "",
    hotspotAgeBucket,
  ].join("|");
  const existing = fireMarkerCache.get(key);
  if (existing) {
    fireMarkerCache.delete(key);
    fireMarkerCache.set(key, existing);
    return existing;
  }
  const request = buildFireMarkers(activeReference, hotspotReference, targetDomain)
    .catch((error) => {
      fireMarkerCache.delete(key);
      throw error;
    });
  fireMarkerCache.set(key, request);
  while (fireMarkerCache.size > MARKER_CACHE_LIMIT) {
    const oldest = fireMarkerCache.keys().next().value;
    if (typeof oldest !== "string") break;
    fireMarkerCache.delete(oldest);
  }
  return request;
}

type ComposedLayer = {
  id: string;
  renderId?: string;
  url: string;
  sourceCacheKey: string;
  opacity: number;
  frame?: Frame;
  stageAligned?: boolean;
};

function rasterLayerId(
  recipeId: string,
  product: Product,
  domain: Domain,
  hourlyLightning = false,
): string {
  let baseId = hourlyLightning
    ? recipeId === "lightning-trail"
      ? "lightning-hour"
      : recipeId === "glm-lightning-trail"
        ? "glm-lightning-hour"
        : recipeId
    : recipeId;
  if (baseId === "model-hgt500") {
    baseId = product.domain === "bc" ? "hrdps-hgt500" : "ecmwf-hgt500";
  } else if (baseId === "model-mslp") {
    baseId = product.domain === "bc" ? "hrdps-mslp" : "ecmwf-mslp";
  }
  const regionKey = REGIONAL_PRODUCT_KEYS[product.id];
  if (
    product.domain === "bc"
    && regionKey
    && [
      "lightning-trail",
      "lightning-hour",
      "hotspots",
      "hrdps-hgt500",
      "hrdps-mslp",
    ].includes(baseId)
  ) {
    const candidate = `${baseId}-region-${regionKey}`;
    if (domain.layers[candidate]?.frames?.length) return candidate;
  }
  return baseId;
}

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
  return optionalLayers[recipe.controlId ?? recipe.id]
    ?? optionalLayers[recipe.id]
    ?? recipe.defaultEnabled
    ?? true;
}

function sameLayerSet(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false;
  const expected = new Set(right);
  return expected.size === right.length
    && new Set(left).size === left.length
    && left.every((id) => expected.has(id));
}

function normalizeLayerChoices(
  current: Record<string, boolean>,
  product: Product,
  products: Product[],
): Record<string, boolean> {
  let next = current;
  const choiceGroups = new Set(
    product.layers.flatMap((recipe) => recipe.choiceGroup ? [recipe.choiceGroup] : []),
  );
  for (const choiceGroup of choiceGroups) {
    const choices = product.layers.filter((recipe) => recipe.choiceGroup === choiceGroup);
    const enabledChoices = choices.filter((recipe) => (
      isProductLayerEnabled(recipe, current, product.layers)
    ));
    if (enabledChoices.length === 1) continue;
    const configuredChoices = products.flatMap((item) => item.layers)
      .filter((recipe) => recipe.choiceGroup === choiceGroup);
    if (enabledChoices.length > 1) {
      // Older saved preferences can contain a newly selected layer without an
      // explicit false for the prior/default layer. Prefer an explicit true
      // over a layer that is enabled only by default, then use product order as
      // the deterministic tie-breaker. Finally rewrite every alias so the
      // invalid multi-selection cannot return on the next refresh.
      const selected = enabledChoices.find((recipe) => (
        current[recipe.controlId ?? recipe.id] === true
        || current[recipe.id] === true
      )) ?? enabledChoices[0];
      if (next === current) next = { ...current };
      for (const candidate of configuredChoices) {
        next[candidate.id] = false;
        next[candidate.controlId ?? candidate.id] = false;
      }
      next[selected.id] = true;
      next[selected.controlId ?? selected.id] = true;
      continue;
    }
    const hasUnavailableSelection = configuredChoices.some((recipe) => (
      current[recipe.controlId ?? recipe.id] === true || current[recipe.id] === true
    ));
    if (!hasUnavailableSelection) continue;
    const fallback = choices.find((recipe) => recipe.defaultEnabled) ?? choices[0];
    if (!fallback) continue;
    if (next === current) next = { ...current };
    for (const candidate of configuredChoices) {
      next[candidate.id] = false;
      next[candidate.controlId ?? candidate.id] = false;
    }
    next[fallback.id] = true;
    next[fallback.controlId ?? fallback.id] = true;
  }
  return next;
}

function activeAnchorLayer(product: Product, optionalLayers: Record<string, boolean>): string {
  const enabled = (id: string) => {
    const recipe = product.layers.find((candidate) => candidate.id === id);
    return Boolean(recipe && isProductLayerEnabled(recipe, optionalLayers, product.layers));
  };
  return ["raw-visir-5min", "raw-visir", "westwx-visir", "raw-ir", "westwx-ir", "eccc-geocolor", "daynight", "ir", "convective", "snowfog", "radar-rain", "ptype", "lightning-trail", "hotspots"]
    .find(enabled) ?? product.anchorLayer;
}

function composeLayers(
  product: Product,
  domain: Domain,
  anchor: Frame,
  catalogBase: string,
  optionalLayers: Record<string, boolean>,
  hourlyLightning = false,
): ComposedLayer[] {
  return product.layers.flatMap((recipe) => {
    if (!isProductLayerEnabled(recipe, optionalLayers, product.layers)) return [];
    const pointsId = pointLayerId(recipe.id);
    if (
      pointsId
      && domain.layers[pointsId]?.frames?.length
      && (
        (LIGHTNING_CONTROLLERS.has(recipe.id) && !usesRasterLightning(product))
        || (recipe.id === "hotspots" && !usesRasterFire(product))
      )
    ) return [];
    const staticLayer = domain.staticLayers[recipe.id];
    if (staticLayer) {
      const url = new URL(staticLayer.path, catalogBase);
      if (staticLayer.revision) url.searchParams.set("v", staticLayer.revision);
      return [{
        id: recipe.id,
        url: url.toString(),
        sourceCacheKey: staticLayer.revision
          ? sourceCacheKey(staticLayer.path, staticLayer.revision)
          : staticLayer.path,
        opacity: recipe.opacity,
      }];
    }
    const renderedLayerId = rasterLayerId(recipe.id, product, domain, hourlyLightning);
    let dynamicLayer = domain.layers[renderedLayerId];
    let frames = dynamicLayer?.frames ?? [];
    if (product.domain === "bc" && recipe.id === "raw-visir-5min") {
      const rapidLayer = domain.layers["raw-visir-5min"];
      const rapidFrame = atOrBefore(
        rapidLayer?.frames ?? [],
        anchor.validTime,
        rapidLayer?.maxAgeMinutes,
      );
      if (rapidFrame) {
        dynamicLayer = rapidLayer;
        frames = [rapidFrame];
      }
    } else if (product.domain === "bc" && recipe.id === "raw-visir") {
      const standardLayer = domain.layers["raw-visir"];
      const nativeLayer = domain.layers["raw-visir-native"];
      const nativeFrame = nearestFrame(nativeLayer?.frames ?? [], anchor.validTime, 2)
        ?? atOrBefore(nativeLayer?.frames ?? [], anchor.validTime, nativeLayer?.maxAgeMinutes);
      const standardFrame = nearestFrame(standardLayer?.frames ?? [], anchor.validTime, 2)
        ?? atOrBefore(standardLayer?.frames ?? [], anchor.validTime, standardLayer?.maxAgeMinutes);
      if (nativeFrame) {
        dynamicLayer = nativeLayer;
        frames = [nativeFrame];
      } else if (standardFrame) {
        dynamicLayer = standardLayer;
        frames = [standardFrame];
      }
    }
    // Trail rasters are regenerated on the radar clock, but their actual
    // observations are ten-minute lightning intervals. Select them by that
    // source interval so VALID and the 0–10/10–20/20–30 minute bins agree.
    const frame = LIGHTNING_CONTROLLERS.has(recipe.id)
      ? atOrBeforeSourceTime(renderedLayerId, frames, anchor.validTime, dynamicLayer?.maxAgeMinutes)
      : atOrBefore(frames, anchor.validTime, dynamicLayer?.maxAgeMinutes);
    if (!frame) return [];
    return [{
      id: recipe.id,
      renderId: renderedLayerId,
      url: frameUrl(frame, catalogBase),
      sourceCacheKey: sourceCacheKey(frame.path, frame.fetchedAt),
      opacity: recipe.opacity,
      frame,
      stageAligned: renderedLayerId.includes("-region-"),
    }];
  });
}

function actualSourceTime(layerId: string, frame: Frame): string {
  if (layerId.includes("lightning") && frame.sourceTimes) {
    const values = Object.values(frame.sourceTimes)
      .filter((value) => Number.isFinite(Date.parse(value)))
      .sort((left, right) => Date.parse(right) - Date.parse(left));
    if (values[0]) return values[0];
  }
  if (["raw-ir", "raw-visir", "raw-visir-5min", "eccc-geocolor"].includes(layerId) && frame.sourceTimes) {
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
    const frameTime = Date.parse(frame.validTime);
    const sourceTime = Date.parse(actualSourceTime(layerId, frame));
    const sourceCount = Object.keys(frame.sourceTimes ?? {}).length;
    if (
      !Number.isFinite(frameTime)
      || frameTime > targetTime
      || !Number.isFinite(sourceTime)
      || sourceTime > targetTime
    ) continue;
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

function playbackStepFactor(frames: Frame[], currentIndex: number): number {
  if (currentIndex < 0 || currentIndex >= frames.length - 1) return 1;
  const minutes = (
    Date.parse(frames[currentIndex + 1].validTime)
    - Date.parse(frames[currentIndex].validTime)
  ) / 60_000;
  return Number.isFinite(minutes) ? Math.max(1, Math.min(6, minutes / 5)) : 1;
}

function layerLabel(layerId: string): string {
  if (layerId.startsWith("radar")) return "RADAR";
  if (layerId.includes("lightning")) return "LTG";
  if (layerId === "smoke") return "SMOKE";
  if (layerId === "ptype") return "PTYPE";
  if (layerId === "site-radar") return "RADAR";
  if (layerId === "hotspots") return "FIRE";
  if (["model-hgt500", "hrdps-hgt500", "ecmwf-hgt500"].includes(layerId)) return "H500";
  if (["model-mslp", "hrdps-mslp", "ecmwf-mslp"].includes(layerId)) return "MSLP";
  if (
    ["daynight", "ir", "convective", "snowfog", "eccc-geocolor", "raw-visir", "raw-visir-5min", "raw-visir-native", "raw-ir"].includes(layerId)
    || layerId.startsWith("westwx-")
  ) return "SAT";
  return layerId.toUpperCase();
}

function sourceLabel(layerId: string): string | null {
  if (layerId.includes("coverage")) return null;
  const label = layerLabel(layerId);
  return ["SAT", "RADAR", "PTYPE", "LTG", "FIRE", "SMOKE", "H500", "MSLP"].includes(label) ? label : null;
}

function layerControlLabel(layerId: string): string {
  if (layerId === "eccc-geocolor") return "MSC GeoColor";
  if (layerId === "ir") return "ECCC IR";
  if (layerId === "daynight") return "ECCC VIS/IR";
  if (layerId === "convective") return "ECCC Convective";
  if (layerId === "snowfog") return "Snow / Fog";
  if (layerId === "westwx-visir") return "NOAA VIS/IR";
  if (layerId === "westwx-ir") return "NOAA IR";
  if (layerId === "raw-visir") return "NOAA VIS/IR";
  if (layerId === "raw-visir-5min") return "NOAA VIS/IR";
  if (layerId === "raw-ir") return "NOAA IR";
  if (layerId === "radar-rain") return "Radar";
  if (layerId === "radar-snow") return "Snow rate";
  if (layerId === "radar-coverage") return "Radar coverage";
  if (layerId === "ptype-coverage") return "Precipitation-type coverage";
  if (layerId === "ptype") return "Precip type";
  if (layerId === "model-hgt500") return "500 hPa Height";
  if (layerId === "model-mslp") return "MSLP";
  if (layerId === "lightning-trail") return "Lightning";
  if (layerId === "lightning") return "Flash density";
  if (layerId === "glm-lightning-trail") return "Lightning";
  if (layerId === "glm-lightning") return "GLM flash bins";
  if (layerId === "smoke") return "Enhanced Smoke";
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

function ZapIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z" />
    </svg>
  );
}

function LightningCanvas({
  markers,
  style,
  className = "",
  label,
}: {
  markers: LightningMarker[];
  style: CSSProperties;
  className?: string;
  label: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const draw = () => {
      const bounds = canvas.getBoundingClientRect();
      if (bounds.width <= 0 || bounds.height <= 0) return;
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.max(1, Math.round(bounds.width * ratio));
      const height = Math.max(1, Math.round(bounds.height * ratio));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      const context = canvas.getContext("2d");
      if (!context) return;
      context.clearRect(0, 0, width, height);
      context.save();
      context.scale(ratio, ratio);
      const size = Math.max(8, Math.min(15, window.innerWidth * 0.0115));
      const colors = ["#fffef0", "#fff29a", "#ffe064", "#f6d451"];
      const opacity = [1, 0.82, 0.56, 0.30];
      for (const marker of markers) {
        context.save();
        context.translate(marker.x / 100 * bounds.width, marker.y / 100 * bounds.height);
        context.scale(size / 24, size / 24);
        context.translate(-12, -12);
        context.globalAlpha = opacity[marker.age];
        if (marker.age === 0) {
          const arrival = context.createRadialGradient(12, 12, 1, 12, 12, 11.5);
          arrival.addColorStop(0, "rgba(255, 255, 246, 0.72)");
          arrival.addColorStop(0.35, "rgba(255, 250, 205, 0.36)");
          arrival.addColorStop(1, "rgba(255, 246, 170, 0)");
          context.fillStyle = arrival;
          context.beginPath();
          context.arc(12, 12, 11.5, 0, Math.PI * 2);
          context.fill();
        }
        context.fillStyle = colors[marker.age];
        if (marker.age === 0) {
          context.shadowColor = "rgba(255, 254, 220, 0.96)";
          context.shadowBlur = 9;
        } else {
          context.shadowColor = "rgba(0, 0, 0, 0.72)";
          context.shadowBlur = 3;
          context.shadowOffsetY = 2;
        }
        context.beginPath();
        context.moveTo(13.4, 1.6);
        context.lineTo(3.2, 12.4);
        context.quadraticCurveTo(2.4, 13.4, 4.0, 14.0);
        context.lineTo(11.8, 14.0);
        context.lineTo(10.0, 21.4);
        context.quadraticCurveTo(9.7, 22.8, 11.0, 21.8);
        context.lineTo(20.8, 11.6);
        context.quadraticCurveTo(21.7, 10.5, 20.0, 10.0);
        context.lineTo(12.1, 10.0);
        context.lineTo(14.0, 2.6);
        context.quadraticCurveTo(14.3, 1.2, 13.4, 1.6);
        context.closePath();
        context.fill();
        context.restore();
      }
      context.restore();
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [markers]);

  return (
    <canvas
      ref={canvasRef}
      className={`point-symbol-layer lightning-canvas ${className}`.trim()}
      style={style}
      role="img"
      aria-label={label}
      data-marker-count={markers.length}
    />
  );
}

function FireCanvas({
  markers,
  style,
}: {
  markers: FireMarker[];
  style: CSSProperties;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const draw = () => {
      const bounds = canvas.getBoundingClientRect();
      if (bounds.width <= 0 || bounds.height <= 0) return;
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.max(1, Math.round(bounds.width * ratio));
      const height = Math.max(1, Math.round(bounds.height * ratio));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      const context = canvas.getContext("2d");
      if (!context) return;
      context.clearRect(0, 0, width, height);
      context.save();
      context.scale(ratio, ratio);

      const drawFlame = (marker: FireMarker) => {
        const active = marker.kind === "active";
        const baseSize = marker.notable
          ? Math.max(14, Math.min(21, window.innerWidth * 0.0165))
          : active
            ? Math.max(8, Math.min(13, window.innerWidth * 0.009))
            : Math.max(7, Math.min(13, window.innerWidth * 0.0102));
        const hotspotColors = ["#ffb05d", "#f08d43", "#c9663b"];
        const hotspotOpacity = [1, 0.72, 0.44];
        const colour = active ? "#ff7956" : hotspotColors[marker.age];
        const opacity = active ? 1 : hotspotOpacity[marker.age];

        context.save();
        context.translate(marker.x / 100 * bounds.width, marker.y / 100 * bounds.height);
        context.scale(baseSize / 24, baseSize / 24);
        context.translate(-12, -12);
        context.globalAlpha = opacity;
        context.shadowColor = active
          ? "rgba(255, 229, 190, 0.72)"
          : "rgba(0, 0, 0, 0.72)";
        context.shadowBlur = active ? 3 : 2;
        context.shadowOffsetY = 1.5;
        context.beginPath();
        context.moveTo(12, 3);
        context.bezierCurveTo(13, 7, 16, 7, 17, 10);
        context.bezierCurveTo(21, 13, 20, 19, 16, 21);
        context.bezierCurveTo(11, 24, 5, 21, 5, 16);
        context.bezierCurveTo(5, 14, 6, 12, 7, 11);
        context.bezierCurveTo(8, 14, 11, 14, 11, 11);
        context.bezierCurveTo(11, 8, 9.5, 7, 9.5, 5);
        context.bezierCurveTo(9.5, 4, 10.5, 3.5, 12, 3);
        context.closePath();
        if (marker.notable) {
          context.strokeStyle = "#ffe45b";
          context.lineWidth = 4;
          context.stroke();
        }
        context.strokeStyle = colour;
        context.fillStyle = colour;
        context.lineWidth = active ? 2 : 2.6;
        if (active) context.fill();
        context.stroke();
        context.restore();

        if (marker.count > 1) {
          context.save();
          context.translate(marker.x / 100 * bounds.width, marker.y / 100 * bounds.height + baseSize * 0.055);
          context.fillStyle = "#3a150b";
          context.font = `800 ${Math.max(7, Math.min(9, baseSize * 0.43))}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`;
          context.textAlign = "center";
          context.textBaseline = "middle";
          context.shadowColor = "rgba(255, 242, 204, 0.8)";
          context.shadowBlur = 2;
          context.fillText(String(marker.count), 0, 0);
          context.restore();
        }
      };

      markers.forEach(drawFlame);
      context.restore();
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [markers]);

  return (
    <canvas
      ref={canvasRef}
      className="point-symbol-layer fire-canvas"
      style={style}
      role="img"
      aria-label="Agency-reported active wildfires and satellite thermal hotspots"
      data-marker-count={markers.length}
    />
  );
}

function FlameIcon({ filled = true, highlighted = false }: { filled?: boolean; highlighted?: boolean }) {
  const path = "M12 3q1 4 4 6.5t3 5.5a1 1 0 0 1-14 0 5 5 0 0 1 1-3 1 1 0 0 0 5 0c0-2-1.5-3-1.5-5q0-2 2.5-4";
  return (
    <svg viewBox="0 0 24 24" fill="none" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {highlighted && <path className="flame-highlight" d={path} />}
      <path d={path} fill={filled ? "currentColor" : "none"} stroke="currentColor" strokeWidth={filled ? "2" : "2.6"} />
    </svg>
  );
}

function LightningLegend({ hourly = false }: { hourly?: boolean }) {
  const rows = [
    ["0–10 min", "age-0"],
    ["10–20 min", "age-1"],
    ["20–30 min", "age-2"],
    [hourly ? "30–60 min" : "30+ min", "age-3"],
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

function TransmissionLegend() {
  return (
    <div className="watershed-legend" aria-label="BC transmission line legend">
      <span className="transmission-symbol" aria-hidden="true" />
      <span>BC transmission line</span>
    </div>
  );
}

function SmokeLegend({ frame }: { frame?: Frame }) {
  return (
    <div className="hotspot-legend" aria-label="Satellite smoke detection confidence legend">
      <div className="hotspot-key-row">
        <span className="smoke-confidence-swatch" style={{ background: "rgba(218, 179, 127, .88)" }} />
        <span>High-confidence smoke tint</span>
      </div>
      <div className="hotspot-key-row">
        <span className="smoke-confidence-swatch" style={{ background: "rgba(165, 143, 120, .72)" }} />
        <span>Medium/low-confidence smoke tint</span>
      </div>
      <p>
        {frame?.availability === "unavailable"
          ? "Unavailable for this scene"
          : "Daylight, sufficiently clear sky only; absence is not proof of clear air"}
      </p>
    </div>
  );
}

function ModelContourLegend({ kind }: { kind: "hgt500" | "mslp" }) {
  const height = kind === "hgt500";
  const rows = height
    ? [["≤ 570 dam", "#c98735", 3.75], ["> 570 dam", "#b95750", 3.75]] as const
    : [["≤ 1024 hPa", "#ef4dff", 1.125], ["> 1024 hPa", "#42a5ff", 1.125]] as const;
  return (
    <div className="hotspot-legend" aria-label={height ? "500 hPa height contour legend" : "Mean sea-level pressure contour legend"}>
      {rows.map(([label, colour, width]) => (
        <div className="hotspot-key-row" key={label}>
          <span
            aria-hidden="true"
            style={{
              display: "inline-block",
              width: "27px",
              borderTop: `${width}px solid ${colour}`,
              boxShadow: "0 1px 0 #151822",
            }}
          />
          <span>{label}</span>
        </div>
      ))}
      <p>{height ? "6 dam interval · bold H/L centres" : "4 hPa interval · light H/L centres"}</p>
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
        <span className="fire-marker active-fire-marker fire-notable legend-marker"><FlameIcon highlighted /></span>
        <span>BCWS Wildfire of Note</span>
      </div>
      {showUsLarge && (
        <div className="hotspot-key-row">
          <span className="fire-marker active-fire-marker fire-notable legend-marker"><FlameIcon highlighted /></span>
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
  const [lightningMarkers, setLightningMarkers] = useState<LightningMarker[]>([]);
  const [ecccFallbackLightningMarkers, setEcccFallbackLightningMarkers] = useState<LightningMarker[]>([]);
  const [fireMarkers, setFireMarkers] = useState<FireMarker[]>([]);
  const [regionMenuOpen, setRegionMenuOpen] = useState(false);
  const [rangeMenuOpen, setRangeMenuOpen] = useState(false);
  const [layersMenuOpen, setLayersMenuOpen] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [pageVisible, setPageVisible] = useState(true);
  const [loadedVideoManifest, setLoadedVideoManifest] = useState<VideoLoopManifest | null>(null);
  const [failedVideoGeneration, setFailedVideoGeneration] = useState("");
  const [videoFallbackReason, setVideoFallbackReason] = useState("");
  const [failedDefaultComposite, setFailedDefaultComposite] = useState<{
    key: string;
    reason: string;
  } | null>(null);
  const presentedVideoIndexRef = useRef(NEWEST_FRAME);
  const lastVideoHudUpdateAtRef = useRef(0);
  const frameCountRef = useRef<HTMLSpanElement>(null);
  const timelineRangeRef = useRef<HTMLInputElement>(null);
  const mapStageRef = useRef<HTMLDivElement>(null);
  const validLineRef = useRef<HTMLParagraphElement>(null);
  const sourceTimesRef = useRef<HTMLParagraphElement>(null);
  const loadedVideoManifestRef = useRef<VideoLoopManifest | null>(null);
  const pendingVideoManifestRef = useRef<VideoLoopManifest | null>(null);
  const fullCatalogLoadRef = useRef("");
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
    const catalogEtags = new Map<string, string>();
    const catalogGenerations = new Map<string, string>();
    async function load() {
      if (loading) return;
      loading = true;
      try {
        const configResponse = await fetch("config.json", { cache: "no-store" });
        if (!configResponse.ok) throw new Error("Site configuration is unavailable.");
        const config = (await configResponse.json()) as SiteConfig;
        const candidates = [config.catalogIndexUrl, config.catalogUrl, config.fallbackCatalogUrl]
          .filter((value): value is string => Boolean(value))
          .map((value) => absoluteUrl(value, configResponse.url))
          .filter((value, index, all) => all.indexOf(value) === index);
        let lastFailure: unknown;
        for (const resolved of candidates) {
          try {
            const previousEtag = catalogEtags.get(resolved);
            if (initialized && previousEtag) {
              try {
                const head = await fetch(resolved, { method: "HEAD", cache: "no-store" });
                const headEtag = head.headers.get("etag");
                if (head.ok && headEtag && headEtag === previousEtag) return;
              } catch {
                // Some development origins do not implement HEAD. A normal GET
                // below remains the compatibility path.
              }
            }
            const response = await fetch(resolved, { cache: "no-store" });
            if (!response.ok) throw new Error(`Loop catalog returned ${response.status}.`);
            const nextCatalog = (await response.json()) as Catalog;
            if (!nextCatalog.products?.length || !nextCatalog.domains) {
              throw new Error("Loop catalog is incomplete.");
            }
            const availableProducts = nextCatalog.products.filter((item) => productHasFrames(nextCatalog, item));
            if (!availableProducts.length) throw new Error("Loop catalog contains no available products.");
            const previousGeneration = catalogGenerations.get(resolved);
            catalogEtags.set(resolved, response.headers.get("etag") ?? "");
            catalogGenerations.set(resolved, nextCatalog.generatedAt);
            if (initialized && previousGeneration === nextCatalog.generatedAt) return;
            if (!cancelled) {
              setCatalog(nextCatalog);
              setCatalogBase(resolved);
              let stored: Partial<ViewerPreferences> = {};
              if (!initialized) {
                try {
                  const currentPreferences = window.sessionStorage.getItem(VIEWER_PREFERENCES_KEY);
                  stored = JSON.parse(
                    currentPreferences
                      ?? window.sessionStorage.getItem(LEGACY_VIEWER_PREFERENCES_KEY)
                      ?? "{}",
                  ) as Partial<ViewerPreferences>;
                  // The new 1× equals the former 2×. Preserve the other legacy
                  // choices while resetting playback to the newly labelled 1×.
                  if (currentPreferences === null) delete stored.speedIndex;
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
                const requestedRange = typeof stored.rangeHours === "number"
                  && RANGE_OPTIONS.includes(stored.rangeHours)
                  ? stored.rangeHours
                  : preferred.defaultHours;
                setRangeHours(requestedRange);
                setSpeedIndex(
                  typeof stored.speedIndex === "number"
                    && stored.speedIndex >= 0
                    && stored.speedIndex < PLAYBACK_SPEEDS.length
                    ? stored.speedIndex
                    : 3,
                );
                if (stored.optionalLayers && typeof stored.optionalLayers === "object") {
                  setOptionalLayers(normalizeLayerChoices(
                    stored.optionalLayers,
                    preferred,
                    nextCatalog.products,
                  ));
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
    loadedVideoManifestRef.current = loadedVideoManifest;
  }, [loadedVideoManifest]);

  useEffect(() => {
    // A visible loop keeps playing on a second monitor even while another
    // browser window or application has focus. Only a genuinely hidden tab is
    // paused to avoid spending decode work on an image nobody can see.
    const updateVisibility = () => {
      setPageVisible(document.visibilityState === "visible");
    };
    updateVisibility();
    document.addEventListener("visibilitychange", updateVisibility);
    return () => document.removeEventListener("visibilitychange", updateVisibility);
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
  const effectiveRangeHours = Math.min(rangeHours, product?.maxHours ?? 168);
  const videoTrack = effectiveRangeHours <= 24 ? "live" : "archive";
  const videoLayerId = activeAnchorId;
  const videoPointer = product && videoLayerId
    ? catalog?.videoProfiles?.[product.id]?.[videoLayerId]?.[videoTrack]
    : undefined;
  const videoPointerKey = videoPointer
    ? `${product?.id}/${videoLayerId}/${videoTrack}/${videoPointer.generation}/${videoPointer.manifestPath}`
    : "";

  useEffect(() => {
    let cancelled = false;
    if (!videoPointer || !videoLayerId || !product || failedVideoGeneration === videoPointer.generation) {
      return;
    }
    const manifestUrl = absoluteUrl(videoPointer.manifestPath, catalogBase);
    void loadVideoLoopManifest(manifestUrl).then((manifest) => {
      if (cancelled) return;
      if (
        manifest.generation !== videoPointer.generation
        || manifest.productId !== product.id
        || manifest.layerId !== videoLayerId
        || manifest.track !== videoTrack
        || !supportsVideoLoop(manifest.media.mimeType)
      ) {
        throw new Error("Video profile does not match the selected loop.");
      }
      const active = loadedVideoManifestRef.current;
      if (
        active
        && active.productId === manifest.productId
        && active.layerId === manifest.layerId
        && active.track === manifest.track
        && active.generation !== manifest.generation
      ) {
        pendingVideoManifestRef.current = manifest;
      } else {
        pendingVideoManifestRef.current = null;
        setLoadedVideoManifest(manifest);
      }
      setFailedVideoGeneration("");
      setVideoFallbackReason("");
    }).catch((reason) => {
      if (!cancelled) {
        setFailedVideoGeneration(videoPointer.generation);
        setVideoFallbackReason(reason instanceof Error ? reason.message : "Video manifest failed.");
      }
    });
    return () => { cancelled = true; };
  }, [catalogBase, failedVideoGeneration, product, videoLayerId, videoPointer, videoPointerKey, videoTrack]);

  const fallbackAnchorFrames = useMemo(() => {
    if (!domain || !product) return [];
    const frames = activeAnchorId === "raw-visir-5min"
      ? mergedFrames(
          domain.layers["raw-visir"]?.frames ?? [],
          domain.layers["raw-visir-5min"]?.frames ?? [],
        )
      : domain.layers[activeAnchorId]?.frames ?? [];
    if (!frames.length) return [];
    return playbackFrames(
      frames,
      effectiveRangeHours,
      product.frameIntervalMinutes,
      product.archiveFrameIntervalMinutes,
    );
  }, [activeAnchorId, domain, effectiveRangeHours, product]);

  const videoFreshEnough = useMemo(() => {
    if (!loadedVideoManifest || !fallbackAnchorFrames.length || !product) return true;
    const videoNewest = Date.parse(
      loadedVideoManifest.frames[loadedVideoManifest.frames.length - 1]?.validTime ?? "",
    );
    const imageNewest = Date.parse(
      fallbackAnchorFrames[fallbackAnchorFrames.length - 1]?.validTime ?? "",
    );
    if (!Number.isFinite(videoNewest) || !Number.isFinite(imageNewest)) return false;
    const expectedMinutes = loadedVideoManifest.track === "archive"
      ? (product.archiveFrameIntervalMinutes ?? 60)
      : (product.frameIntervalMinutes ?? 10);
    // A slow/failed encoder must never make current imagery look stale. Keep
    // video only while it is within one nominal frame (plus scan tolerance) of
    // the image fallback that the rapid ingest has already published.
    return imageNewest - videoNewest <= (expectedMinutes * 60_000 + 120_000);
  }, [fallbackAnchorFrames, loadedVideoManifest, product]);

  const candidateVideoManifest = useMemo(() => (
    loadedVideoManifest
      && videoPointer
      && videoLayerId
      && loadedVideoManifest.productId === product?.id
      && loadedVideoManifest.layerId === videoLayerId
      && loadedVideoManifest.track === videoTrack
      && videoFreshEnough
      && failedVideoGeneration !== loadedVideoManifest.generation
      ? loadedVideoManifest
      : null
  ), [failedVideoGeneration, loadedVideoManifest, product?.id, videoFreshEnough, videoLayerId, videoPointer, videoTrack]);
  const enabledVideoLayerIds = useMemo(() => product?.layers
    .filter((recipe) => isProductLayerEnabled(recipe, optionalLayers, product.layers))
    .map((recipe) => recipe.id) ?? [], [optionalLayers, product]);
  const candidateDefaultComposite = candidateVideoManifest?.defaultComposite;
  const candidateDefaultCompositeKey = candidateDefaultComposite && candidateVideoManifest
    ? `${candidateVideoManifest.generation}/${candidateDefaultComposite.id}/${candidateDefaultComposite.media.path}`
    : "";
  const activeDefaultComposite = candidateDefaultComposite
    && candidateDefaultCompositeKey !== failedDefaultComposite?.key
    && sameLayerSet(enabledVideoLayerIds, candidateDefaultComposite.layerIds)
    ? candidateDefaultComposite
    : undefined;
  const playbackVideoManifest = useMemo<VideoLoopManifest | null>(() => (
    candidateVideoManifest && activeDefaultComposite
      ? {
          ...candidateVideoManifest,
          media: activeDefaultComposite.media,
          mediaViewport: activeDefaultComposite.mediaViewport,
        }
      : candidateVideoManifest
  ), [activeDefaultComposite, candidateVideoManifest]);
  const videoManifestFrames = useMemo(
    () => candidateVideoManifest
      ? selectVideoFrames(candidateVideoManifest, effectiveRangeHours)
      : [],
    [candidateVideoManifest, effectiveRangeHours],
  );
  const videoAnchorFrames = useMemo<Frame[]>(() => videoManifestFrames.map((frame) => ({
    validTime: frame.validTime,
    path: frame.sourcePath,
    source: "NOAA GOES-18",
    sourceLayer: frame.encodedSourceLayer,
    fetchedAt: frame.sourceFetchedAt,
    sourceTimes: frame.sourceTimes ?? { satellite: frame.sourceValidTime },
  })), [videoManifestFrames]);
  // Proxy planning intentionally stays memoized across media-clock frame
  // commits. The immutable manifest freezes each proxy choice at encode time;
  // consulting the newer mutable catalog here could pair an older satellite
  // video with a late-arriving radar/lightning correction that it cannot load.
  // eslint-disable-next-line react-hooks/preserve-manual-memoization
  const videoPlans = useMemo<VideoCompositeFramePlan[]>(() => {
    if (
      !playbackVideoManifest
      || !product
      || !videoLayerId
      || videoAnchorFrames.length !== videoManifestFrames.length
    ) return [];
    const satelliteRecipeIndex = product.layers.findIndex((recipe) => recipe.id === videoLayerId);
    if (satelliteRecipeIndex < 0) return [];
    const recipeById = new Map(product.layers.map((recipe, index) => [recipe.id, { recipe, index }]));
    const plans: VideoCompositeFramePlan[] = [];
    for (let index = 0; index < videoManifestFrames.length; index += 1) {
      const manifestFrame = videoManifestFrames[index];
      const underlays: VideoCanvasProxyLayer[] = [];
      const overlays: VideoCanvasProxyLayer[] = [];
      if (activeDefaultComposite) {
        plans.push({
          frame: manifestFrame,
          cacheKey: `composite:${activeDefaultComposite.id}:${activeDefaultComposite.media.path}`,
          underlays,
          overlays,
        });
        continue;
      }
      for (const selection of manifestFrame.proxyLayers) {
        const configured = recipeById.get(selection.id);
        if (
          !configured
          || !isProductLayerEnabled(configured.recipe, optionalLayers, product.layers)
        ) continue;
        const proxy = playbackVideoManifest.proxies[selection.sourceKey];
        if (
          !proxy
          || proxy.width !== playbackVideoManifest.width
          || proxy.height !== playbackVideoManifest.height
        ) return [];
        const layer = {
          url: absoluteUrl(proxy.path, catalogBase),
          opacity: configured.recipe.opacity,
          width: proxy.width,
          height: proxy.height,
        };
        if (configured.index < satelliteRecipeIndex) underlays.push(layer);
        else overlays.push(layer);
      }
      const cacheKey = [
        ...underlays.map((layer) => `u:${layer.url}:${layer.opacity}`),
        ...overlays.map((layer) => `o:${layer.url}:${layer.opacity}`),
      ].join("|");
      plans.push({
        frame: manifestFrame,
        cacheKey,
        underlays,
        overlays,
      });
    }
    return plans;
  }, [
    activeDefaultComposite,
    catalogBase,
    optionalLayers,
    playbackVideoManifest,
    videoLayerId,
    product,
    videoAnchorFrames,
    videoManifestFrames,
  ]);
  const videoModeReady = Boolean(
    playbackVideoManifest
    && videoPlans.length >= 2
    && videoPlans.length === videoAnchorFrames.length,
  );
  const videoHudSourceTimes = useMemo(() => videoPlans.map((plan) => (
    plan.frame.proxyLayers.filter((selection) => {
      const recipe = product?.layers.find((candidate) => candidate.id === selection.id);
      return Boolean(recipe && product && isProductLayerEnabled(recipe, optionalLayers, product.layers));
    }).flatMap((selection): { label: string; validTime: string }[] => {
      const label = sourceLabel(selection.id);
      return label && selection.sourceValidTime
        ? [{ label, validTime: selection.sourceValidTime }]
        : [];
    }).concat([{ label: "SAT", validTime: plan.frame.sourceValidTime }])
      .filter((item, index, all) => all.findIndex((candidate) => (
        candidate.label === item.label && candidate.validTime === item.validTime
      )) === index)
      .map((item) => `${item.label} ${shortClock(item.validTime)}`)
      .join(" · ")
  )), [optionalLayers, product, videoPlans]);

  useEffect(() => {
    if (catalog?.catalogMode !== "index" || !catalog.fullCatalogPath || !product) return;
    const selectedManifestLoaded = Boolean(
      loadedVideoManifest
      && videoPointer
      && loadedVideoManifest.generation === videoPointer.generation
      && loadedVideoManifest.productId === product.id
      && loadedVideoManifest.layerId === videoLayerId
      && loadedVideoManifest.track === videoTrack
    );
    const needsFullCatalog = !videoPointer
      || failedVideoGeneration === videoPointer.generation
      || (selectedManifestLoaded && (!videoFreshEnough || !videoModeReady));
    if (!needsFullCatalog) return;

    const fullCatalogUrl = absoluteUrl(catalog.fullCatalogPath, catalogBase);
    const requestKey = `${catalog.generatedAt}|${fullCatalogUrl}`;
    if (fullCatalogLoadRef.current === requestKey) return;
    fullCatalogLoadRef.current = requestKey;
    let cancelled = false;
    void fetch(fullCatalogUrl, { cache: "no-store" }).then(async (response) => {
      if (!response.ok) throw new Error(`Full loop catalog returned ${response.status}.`);
      const fullCatalog = (await response.json()) as Catalog;
      if (!fullCatalog.products?.length || !fullCatalog.domains) {
        throw new Error("Full loop catalog is incomplete.");
      }
      if (!cancelled) {
        setCatalog({
          ...fullCatalog,
          catalogMode: "full",
          fullCatalogPath: catalog.fullCatalogPath,
        });
        setCatalogBase(fullCatalogUrl);
      }
    }).catch((reason) => {
      if (!cancelled) {
        setVideoFallbackReason(
          reason instanceof Error ? reason.message : "Full image catalog failed.",
        );
      }
    });
    return () => { cancelled = true; };
  }, [
    catalog,
    catalogBase,
    failedVideoGeneration,
    loadedVideoManifest,
    product,
    videoFreshEnough,
    videoLayerId,
    videoModeReady,
    videoPointer,
    videoTrack,
  ]);
  const anchorFrames = videoModeReady ? videoAnchorFrames : fallbackAnchorFrames;

  const availableRangeOptions = useMemo(
    () => RANGE_OPTIONS.filter((hours) => hours <= (product?.maxHours ?? 168)),
    [product?.maxHours],
  );

  const speed = PLAYBACK_SPEEDS[speedIndex] ?? 1;
  const currentFrameIndex = Math.min(frameIndex, Math.max(0, anchorFrames.length - 1));
  const stepFactor = playbackStepFactor(anchorFrames, currentFrameIndex);
  const isAnimating = playing && pageVisible && anchorFrames.length > 1;
  const anchor = anchorFrames[currentFrameIndex];
  const lightningController = product?.layers.find((recipe) => LIGHTNING_CONTROLLERS.has(recipe.id));
  const lightningPointsId = lightningController ? pointLayerId(lightningController.id) : undefined;
  // GOES-18 GLM has the same physical coverage in both broad views. Reuse the
  // dense North America point archive on the dateline-centred Pacific grid;
  // the client remaps those normalized Mercator coordinates without another
  // network product or a duplicate R2 archive.
  const lightningPointDomain = domain?.id === "north-pacific"
    ? catalog?.domains["north-america"]
    : domain;
  const lightningPointReferences = useMemo(() => {
    if (
      usesRasterLightning(product)
      || !product
      || !lightningPointDomain
      || !anchor
      || !catalogBase
      || !lightningController
      || !lightningPointsId
      || !isProductLayerEnabled(lightningController, optionalLayers, product.layers)
    ) return [];
    return pointFrameReferences(
      lightningPointDomain.layers[lightningPointsId]?.frames ?? [],
      anchor.validTime,
      catalogBase,
      32,
      3,
    );
  }, [anchor, catalogBase, lightningController, lightningPointDomain, lightningPointsId, optionalLayers, product]);
  const ecccFallbackPointReferences = useMemo(() => {
    if (
      usesRasterLightning(product)
      || !catalog
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
      : product.domain === "bc"
        ? catalog?.domains.bc
        : catalog?.domains["north-america"];
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
    return resilientActiveFireFrameReferences(
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
        const presented = presentedVideoIndexRef.current;
        const base = videoModeReady && presented !== NEWEST_FRAME ? presented : current;
        const safeCurrent = Math.min(Math.max(0, base), anchorFrames.length - 1);
        const next = (safeCurrent + amount + anchorFrames.length) % anchorFrames.length;
        presentedVideoIndexRef.current = next;
        lastVideoHudUpdateAtRef.current = 0;
        return next;
      });
    },
    [anchorFrames.length, videoModeReady],
  );

  const handleVideoFramePresented = useCallback((index: number) => {
    presentedVideoIndexRef.current = index;
    const now = performance.now();
    if (
      index !== 0
      && index !== anchorFrames.length - 1
      && now - lastVideoHudUpdateAtRef.current < VIDEO_HUD_UPDATE_INTERVAL_MS
    ) return;
    lastVideoHudUpdateAtRef.current = now;
    const frame = videoAnchorFrames[index];
    const plan = videoPlans[index];
    if (!frame || !plan || !product) return;
    const times = videoHudSourceTimes[index] ?? "";
    if (frameCountRef.current) {
      frameCountRef.current.textContent = `${index + 1} / ${anchorFrames.length}`;
    }
    if (timelineRangeRef.current) timelineRangeRef.current.value = String(index);
    if (validLineRef.current) {
      validLineRef.current.textContent = `VALID ${utcClock(frame.validTime)} UTC · ${localClock(frame.validTime)}`;
    }
    if (sourceTimesRef.current) sourceTimesRef.current.textContent = times;
    if (mapStageRef.current) {
      mapStageRef.current.setAttribute(
        "aria-label",
        `${product.title}, valid ${utcClock(frame.validTime)} UTC. ${times}`,
      );
    }
  }, [anchorFrames.length, product, videoAnchorFrames, videoHudSourceTimes, videoPlans]);

  const togglePlayback = useCallback(() => {
    if (isAnimating && videoModeReady) {
      const presented = presentedVideoIndexRef.current;
      if (presented >= 0 && presented < anchorFrames.length) setFrameIndex(presented);
    }
    setPlaying((value) => !value);
  }, [anchorFrames.length, isAnimating, videoModeReady]);

  const handleVideoFailure = useCallback((generation: string) => {
    setFailedVideoGeneration(generation);
  }, []);

  const activeVideoGeneration = candidateVideoManifest?.generation ?? "";
  const handleActiveVideoFailure = useCallback((message: string) => {
    const pending = pendingVideoManifestRef.current;
    if (pending && pending.generation !== activeVideoGeneration) {
      pendingVideoManifestRef.current = null;
      loadedVideoManifestRef.current = pending;
      setLoadedVideoManifest(pending);
      return;
    }
    if (activeDefaultComposite && candidateDefaultCompositeKey) {
      setFailedDefaultComposite({ key: candidateDefaultCompositeKey, reason: message });
      setVideoFallbackReason("");
      return;
    }
    if (activeVideoGeneration) {
      setVideoFallbackReason(message);
      handleVideoFailure(activeVideoGeneration);
    }
  }, [
    activeDefaultComposite,
    activeVideoGeneration,
    candidateDefaultCompositeKey,
    handleVideoFailure,
  ]);

  const handleVideoLoopBoundary = useCallback(() => {
    const pending = pendingVideoManifestRef.current;
    if (!pending) return;
    pendingVideoManifestRef.current = null;
    loadedVideoManifestRef.current = pending;
    setLoadedVideoManifest(pending);
    setFrameIndex(0);
  }, []);

  useEffect(() => {
    let cancelled = false;
    if (!lightningPointReferences.length) {
      const clearMarkers = window.setTimeout(() => {
        setLightningMarkers((current) => current.length ? [] : current);
      }, 0);
      return () => window.clearTimeout(clearMarkers);
    }

    void cachedLightningMarkers(
      lightningPointReferences,
      "",
      domain?.id === "north-america" ? 700 : 1_200,
      product?.domain,
    ).then((markers) => {
      if (!cancelled) setLightningMarkers(markers);
    }).catch(() => {
      if (!cancelled) setLightningMarkers((current) => current.length ? [] : current);
    });
    return () => { cancelled = true; };
  }, [domain?.id, lightningPointReferences, product?.domain]);

  useEffect(() => {
    let cancelled = false;
    if (!ecccFallbackPointReferences.length) {
      const clearMarkers = window.setTimeout(() => {
        setEcccFallbackLightningMarkers((current) => current.length ? [] : current);
      }, 0);
      return () => window.clearTimeout(clearMarkers);
    }

    void cachedLightningMarkers(ecccFallbackPointReferences, "eccc-", 250, "bc").then((markers) => {
      if (!cancelled) setEcccFallbackLightningMarkers(markers);
    }).catch(() => {
      if (!cancelled) {
        setEcccFallbackLightningMarkers((current) => current.length ? [] : current);
      }
    });
    return () => { cancelled = true; };
  }, [ecccFallbackPointReferences]);

  useEffect(() => {
    let cancelled = false;
    if (usesRasterFire(product)) {
      const clearMarkers = window.setTimeout(() => {
        setFireMarkers((current) => current.length ? [] : current);
      }, 0);
      return () => window.clearTimeout(clearMarkers);
    }
    const hotspotReference = firePointReferences[0];
    const activeReference = activeFirePointReferences[0];
    if (!hotspotReference && !activeReference) {
      const clearMarkers = window.setTimeout(() => {
        setFireMarkers((current) => current.length ? [] : current);
      }, 0);
      return () => window.clearTimeout(clearMarkers);
    }

    void cachedFireMarkers(activeReference, hotspotReference, product?.domain ?? "bc").then((markers) => {
      if (!cancelled) setFireMarkers(markers);
    }).catch(() => {
      if (!cancelled) setFireMarkers((current) => current.length ? [] : current);
    });
    return () => { cancelled = true; };
  }, [activeFirePointReferences, firePointReferences, product]);

  useEffect(() => {
    if (videoModeReady || !isAnimating || !catalog || !domain || !product || !catalogBase) return;
    const pointReferencesFor = (candidate: Frame): PointFrameReference[] => {
      const references = product.layers.flatMap((recipe) => {
        if (!isProductLayerEnabled(recipe, optionalLayers, product.layers)) return [];
        if (usesRasterLightning(product) && LIGHTNING_CONTROLLERS.has(recipe.id)) return [];
        if (usesRasterFire(product) && recipe.id === "hotspots") return [];
        const pointsId = pointLayerId(recipe.id);
        if (!pointsId) return [];
        const nativePointDomain = domain.layers[pointsId]?.frames?.length ? domain : undefined;
        const pointDomain = recipe.id === "hotspots"
          ? nativePointDomain ?? (product.domain === "bc" ? catalog.domains.bc : catalog.domains["north-america"])
          : product.domain === "north-pacific"
            ? catalog.domains["north-america"]
            : domain;
        return recipe.id === "hotspots"
          ? rollingPointFrameReferences(
              pointDomain?.layers[pointsId]?.frames ?? [],
              candidate.validTime,
              catalogBase,
              6 * 60,
            )
          : pointFrameReferences(
              pointDomain?.layers[pointsId]?.frames ?? [],
              candidate.validTime,
              catalogBase,
              32,
              3,
            );
      });
      const fireRecipe = product.layers.find((recipe) => recipe.id === "hotspots");
      if (
        !usesRasterFire(product)
        && fireRecipe
        && isProductLayerEnabled(fireRecipe, optionalLayers, product.layers)
      ) {
        const activePointDomain = domain.layers["active-fire-points"]?.frames?.length
          ? domain
          : catalog.domains["north-america"];
        references.push(...resilientActiveFireFrameReferences(
          activePointDomain?.layers["active-fire-points"]?.frames ?? [],
          candidate.validTime,
          catalogBase,
          6 * 60,
        ));
      }
      if (
        !usesRasterLightning(product)
        && product.domain === "north-america"
        && product.layers.some((recipe) => (
          LIGHTNING_CONTROLLERS.has(recipe.id)
          && isProductLayerEnabled(recipe, optionalLayers, product.layers)
        ))
      ) {
        references.push(...pointFrameReferences(
          catalog.domains.bc?.layers["lightning-points"]?.frames ?? [],
          candidate.validTime,
          catalogBase,
          32,
          3,
        ));
      }
      return references;
    };
    const lookaheadCount = 3;
    const lookahead = Array.from({ length: Math.min(lookaheadCount, anchorFrames.length - 1) }, (_, offset) => {
      const index = (currentFrameIndex + offset + 1) % anchorFrames.length;
      const candidate = anchorFrames[index];
      const layers = composeLayers(
        product,
        domain,
        candidate,
        catalogBase,
        optionalLayers,
        effectiveRangeHours > 24,
      );
      return {
        // Satellite, radar, smoke, model and lightning rasters must be decoded
        // before the display clock moves. In particular, advancing without a
        // ready lightning raster makes the two-slot image buffer retain an
        // older overlay for several fast frames. Fire and coverage remain
        // non-blocking: fire changes slowly, while the radar/ptype data raster
        // is authoritative for the coverage observation.
        criticalUrls: layers
          .filter((layer) => (
            layer.frame
            && layer.id !== "hotspots"
            && !layer.id.endsWith("coverage")
          ))
          .map((layer) => layer.url),
        lightningUrls: layers
          .filter((layer) => layer.frame && LIGHTNING_CONTROLLERS.has(layer.id))
          .map((layer) => layer.url),
        pointReferences: pointReferencesFor(candidate),
      };
    });
    if (!lookahead.length) return;

    // Prime the immediately following frame at high priority and retain a
    // small low-priority buffer. Starting too many full-resolution rasters at
    // once competes with the frame that actually needs to be displayed.
    lookahead.forEach((candidate, index) => {
      // Decode all rasters for the next two timestamps. Lightning is small on
      // the wire, so keep three timestamps ready without starting three copies
      // of the much larger satellite and radar backgrounds.
      if (index < 2) {
        candidate.criticalUrls.forEach((url) => void preloadImageFrame(url, index === 0 ? "high" : "low"));
      } else {
        candidate.lightningUrls.forEach((url) => void preloadImageFrame(url, "low"));
      }
      candidate.pointReferences.forEach((reference) => preloadPointFrame(reference.url));
    });
    const finalFrame = currentFrameIndex === anchorFrames.length - 1;
    const delay = (110 * stepFactor + (finalFrame ? 215 : 0)) / speed;
    let cancelled = false;
    let timer: number | undefined;
    const advanceWhenReady = async (attempt = 0) => {
      const ready = await Promise.all(
        lookahead[0].criticalUrls.map((url) => preloadImageFrame(url, "high")),
      );
      if (cancelled) return;
      if (ready.some((loaded) => !loaded) && attempt < PLAYBACK_IMAGE_RETRIES) {
        timer = window.setTimeout(
          () => { void advanceWhenReady(attempt + 1); },
          PLAYBACK_IMAGE_RETRY_DELAY_MS,
        );
        return;
      }
      timer = window.setTimeout(() => advance(1), delay);
    };
    void advanceWhenReady();

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
    effectiveRangeHours,
    isAnimating,
    optionalLayers,
    product,
    speed,
    stepFactor,
    videoModeReady,
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
        togglePlayback();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [advance, togglePlayback]);

  useEffect(() => {
    if (!sourcesOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSourcesOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [sourcesOpen]);

  if (error) {
    return (
      <main className="app-shell">
        <h1 className="brand">Real-Time Wx Display</h1>
        <div className="error-panel" role="alert">{error}</div>
      </main>
    );
  }
  if (!catalog || !product || !domain) return <main className="loading-page">Loading observational loops…</main>;

  const isLayerEnabled = (recipe: ProductLayer) =>
    isProductLayerEnabled(recipe, optionalLayers, product.layers);
  const composedLayers = anchor
    ? composeLayers(
        product,
        domain,
        anchor,
        catalogBase,
        optionalLayers,
        effectiveRangeHours > 24,
      )
    : [];
  const activeVideoProxyLayers = videoModeReady && videoPlans[currentFrameIndex]
    ? videoPlans[currentFrameIndex].frame.proxyLayers.filter((selection) => {
        const recipe = product.layers.find((candidate) => candidate.id === selection.id);
        return Boolean(recipe && isLayerEnabled(recipe));
      })
    : [];
  const pointSourceTimes = (videoModeReady ? [] : [
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
  ]).filter((item): item is { label: string; validTime: string } => Boolean(item));
  const sourceTimes = (videoModeReady
    ? activeVideoProxyLayers.flatMap((selection): { label: string; validTime: string }[] => {
        const label = sourceLabel(selection.id);
        return label && selection.sourceValidTime
          ? [{ label, validTime: selection.sourceValidTime }]
          : [];
      })
    : composedLayers
        .filter((item): item is typeof item & { frame: Frame } => "frame" in item && Boolean(item.frame))
        .map((item) => ({ label: sourceLabel(item.id), validTime: actualSourceTime(item.id, item.frame) }))
        .filter((item): item is { label: string; validTime: string } => Boolean(item.label)))
    .concat(
      videoModeReady && videoPlans[currentFrameIndex]
        ? [{ label: "SAT", validTime: videoPlans[currentFrameIndex].frame.sourceValidTime }]
        : [],
    )
    .concat(pointSourceTimes)
    .filter((item, index, all) => all.findIndex((candidate) => candidate.label === item.label && candidate.validTime === item.validTime) === index)
    .map((item) => `${item.label} ${shortClock(item.validTime)}`)
    .join(" · ");
  const composedLayerIds = new Set(
    videoModeReady
      ? ["base-dark", videoLayerId, ...activeVideoProxyLayers.map((layer) => layer.id)]
      : composedLayers.map((layer) => layer.id),
  );
  if (!videoModeReady && lightningController && (lightningPointReferences.length || ecccFallbackPointReferences.length)) {
    composedLayerIds.add(lightningController.id);
  }
  if (!videoModeReady && fireController && (firePointReferences.length || activeFirePointReferences.length)) {
    composedLayerIds.add(fireController.id);
  }
  const satelliteFilter = ["Overlay", "Broad"].includes(product.group)
    && (composedLayerIds.has("radar-rain") || composedLayerIds.has("ptype"))
    ? "saturate(0.52) brightness(0.78) contrast(1.06)"
    : undefined;
  const missingLayers = product.layers
    .filter((recipe) => (
      isLayerEnabled(recipe)
      && !recipe.enabledWith
      && !domain.staticLayers[recipe.id]
      && !composedLayerIds.has(recipe.id)
    ))
    .map((recipe) => layerControlLabel(recipe.id))
    .filter((label, index, all) => all.indexOf(label) === index);
  const hasCoverage = [...composedLayerIds].some((layerId) => layerId.includes("coverage"));
  const optional = product.layers.filter((layer) => layer.optional);
  const commonOptional = optional.filter((layer) => layer.controlSection !== "regional-satellite");
  const regionalSatelliteOptional = optional.filter((layer) => layer.controlSection === "regional-satellite");
  const activeLayerLabels = optional
    .filter(isLayerEnabled)
    .map((layer) => layerControlLabel(layer.id));
  const toggleOptionalLayer = (layer: ProductLayer, checked: boolean) => {
    setOptionalLayers((current) => {
      const next = { ...current };
      const allProductLayers = catalog.products.flatMap((item) => item.layers);
      if (checked && layer.choiceGroup) {
        for (const peer of allProductLayers) {
          if (peer.choiceGroup !== layer.choiceGroup) continue;
          next[peer.id] = false;
          next[peer.controlId ?? peer.id] = false;
        }
      }
      const controlId = layer.controlId ?? layer.id;
      for (const peer of allProductLayers) {
        if ((peer.controlId ?? peer.id) === controlId) next[peer.id] = checked;
      }
      next[controlId] = checked;
      return next;
    });
    setFrameIndex(NEWEST_FRAME);
    setPlaying(true);
  };
  const visibleLegends = product.legends.filter((legendId) => {
    const recipe = product.layers.find((layer) => layer.id === legendLayerId(legendId));
    return !recipe || isLayerEnabled(recipe);
  });
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
          <h1 className="brand">Real-Time Wx Display</h1>
        </div>
      </header>

      <section className="viewer-grid" aria-label={product.title}>
        <div
          className="map-column"
          style={{
            "--map-aspect": `${mapAspect}`,
            "--map-max-width": `calc(${mapAspect * 100}dvh - ${mapAspect * 58}px)`,
          } as CSSProperties}
        >
          <div className="timeline-panel">
            <div className="transport-row">
              <div className="transport-actions">
                <button className="control-button" type="button" aria-label="Previous frame" disabled={anchorFrames.length < 2} onClick={() => { setPlaying(false); advance(-1); }}>‹</button>
                <button className="control-button primary play-control" type="button" aria-label={isAnimating ? "Pause animation" : "Play animation"} aria-pressed={isAnimating} disabled={anchorFrames.length < 2} onClick={togglePlayback}>
                  <span className={isAnimating ? "pause-icon" : "play-icon"} aria-hidden="true" />
                </button>
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
              <div
                className={`expanding-selector product-switcher${regionMenuOpen ? " is-open" : ""}`}
                onMouseLeave={() => setRegionMenuOpen(false)}
              >
                <button
                  className="selector-current"
                  type="button"
                  aria-label={`Region: ${product.shortTitle}`}
                  aria-expanded={regionMenuOpen}
                  onClick={() => setRegionMenuOpen((open) => !open)}
                >
                  <span className="selector-value">{product.shortTitle}</span>
                  <span className="selector-chevron" aria-hidden="true">⌃</span>
                </button>
                <div className="selector-options product-menu" role="group" aria-label="Loop products">
                  {availableProducts.map((item) => (
                    <button
                      className="product-button"
                      type="button"
                      aria-pressed={item.id === product.id}
                      key={item.id}
                      onClick={(event) => {
                        setPlaying(true);
                        setOptionalLayers((current) => normalizeLayerChoices(
                          current,
                          item,
                          availableProducts,
                        ));
                        setProductId(item.id);
                        setFrameIndex(NEWEST_FRAME);
                        setRegionMenuOpen(false);
                        event.currentTarget.blur();
                      }}
                    >
                      {item.shortTitle}
                    </button>
                  ))}
                </div>
              </div>
              <div
                className={`expanding-selector range-selector${rangeMenuOpen ? " is-open" : ""}`}
                onMouseLeave={() => setRangeMenuOpen(false)}
              >
                <button
                  className="selector-current"
                  type="button"
                  aria-label={`Time span: ${effectiveRangeHours === 168 ? "7 days" : `${effectiveRangeHours} hours`}`}
                  aria-expanded={rangeMenuOpen}
                  onClick={() => setRangeMenuOpen((open) => !open)}
                >
                  <span className="selector-value">{effectiveRangeHours === 168 ? "7 d" : `${effectiveRangeHours} h`}</span>
                  <span className="selector-chevron" aria-hidden="true">⌃</span>
                </button>
                <div className="selector-options range-actions" role="group" aria-label="Archive range">
                  {availableRangeOptions.map((hours) => (
                    <button className="range-button" type="button" aria-pressed={effectiveRangeHours === hours} key={hours} onClick={(event) => { setRangeHours(hours); setFrameIndex(NEWEST_FRAME); setPlaying(true); setRangeMenuOpen(false); event.currentTarget.blur(); }}>
                      {hours === 168 ? "7 d" : `${hours} h`}
                    </button>
                  ))}
                </div>
              </div>
              <div className="timeline-metadata">
                <span ref={frameCountRef} className="frame-count">{anchorFrames.length ? `${currentFrameIndex + 1} / ${anchorFrames.length}` : "0 / 0"}</span>
                <span className="archive-span">{selectedArchiveSpan}</span>
              </div>
            </div>
          </div>
          <div className="timeline-scrubber">
            <input
              ref={timelineRangeRef}
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
            ref={mapStageRef}
            className="map-stage"
            data-renderer={videoModeReady ? "video" : "images"}
            data-video-fallback={videoFallbackReason || undefined}
            data-composite-preset={activeDefaultComposite?.id ?? "dynamic"}
            data-composite-path={activeDefaultComposite?.media.path || undefined}
            data-composite-fallback={
              candidateDefaultCompositeKey === failedDefaultComposite?.key
                ? failedDefaultComposite.reason
                : undefined
            }
            role="img"
            aria-label={`${product.title}${anchor ? `, valid ${utcClock(anchor.validTime)} UTC. ${sourceTimes}` : ", no frames available"}`}
          >
            {!anchor && <div className="map-loading">No frames are available for this product yet.</div>}
            {videoModeReady && playbackVideoManifest ? (
              <VideoCompositeStage
                manifest={playbackVideoManifest}
                mediaUrl={absoluteUrl(playbackVideoManifest.media.path, catalogBase)}
                plans={videoPlans}
                requestedIndex={currentFrameIndex}
                playing={isAnimating}
                speed={speed}
                satelliteFilter={activeDefaultComposite ? undefined : satelliteFilter}
                compositePresetId={activeDefaultComposite?.id}
                compositeMediaPath={activeDefaultComposite?.media.path}
                onFramePresented={handleVideoFramePresented}
                onFailure={handleActiveVideoFailure}
                onLoopBoundary={handleVideoLoopBoundary}
                key={`${product.id}-${playbackVideoManifest.layerId}-${playbackVideoManifest.track}-${playbackVideoManifest.width}x${playbackVideoManifest.height}-${activeDefaultComposite ? `composite:${activeDefaultComposite.id}:${activeDefaultComposite.media.path}` : "dynamic"}`}
              />
            ) : composedLayers.map((layer) => (
              <StableMapImage
                className="map-layer"
                src={layer.url}
                layerId={layer.renderId ?? layer.id}
                key={`${product.id}-${layer.id}`}
                style={{
                  ...(layer.stageAligned ? FULL_LAYER_STYLE : cropStyle),
                  opacity: layer.opacity,
                  filter: ["Overlay", "Broad"].includes(product.group)
                    && ["ir", "daynight", "convective", "snowfog", "eccc-geocolor", "raw-visir", "raw-visir-5min", "raw-ir", "westwx-visir", "westwx-ir"].includes(layer.id)
                    ? satelliteFilter
                    : undefined,
                }}
              />
            ))}
            {!videoModeReady && lightningMarkers.length > 0 && (
              <LightningCanvas
                markers={lightningMarkers}
                style={cropStyle}
                label="Recent lightning activity; brighter bolts are newer"
              />
            )}
            {!videoModeReady && ecccFallbackLightningMarkers.length > 0 && product.domain === "north-america" && (
              <LightningCanvas
                markers={ecccFallbackLightningMarkers}
                style={BC_ON_NORTH_AMERICA_STYLE}
                className="eccc-north-fallback"
                label="Recent ECCC lightning activity in northern British Columbia"
              />
            )}
            {!videoModeReady && fireMarkers.length > 0 && (
              <FireCanvas markers={fireMarkers} style={cropStyle} />
            )}
            {anchor && (
              <div className="map-status">
                <p ref={validLineRef} className="valid-line">VALID {utcClock(anchor.validTime)} UTC · {localClock(anchor.validTime)}</p>
                <p ref={sourceTimesRef} className="source-times">{sourceTimes || `SOURCE ${shortClock(anchor.validTime)}`}</p>
                {missingLayers.length > 0 && (
                  <p className="source-warning">Unavailable: {missingLayers.join(", ")}</p>
                )}
              </div>
            )}
            {hasCoverage && (
              <div className="coverage-key"><span className="hatch-swatch" /> Radar coverage</div>
            )}
          </div>
        </div>

        <aside className="legend-rail" aria-label="Map legends">
          {optional.length > 0 && (
            <div
              className={`layer-selector${layersMenuOpen ? " is-open" : ""}`}
              onMouseLeave={(event) => {
                setLayersMenuOpen(false);
                const focused = document.activeElement;
                if (focused instanceof HTMLElement && event.currentTarget.contains(focused)) {
                  focused.blur();
                }
              }}
            >
              <button
                className="layers-summary"
                type="button"
                aria-expanded={layersMenuOpen}
                onClick={() => setLayersMenuOpen((open) => !open)}
              >
                <span className="layers-summary-heading">
                  <span className="selector-label">Layers</span>
                  <span className="layers-count">{activeLayerLabels.length} on</span>
                </span>
                <span className="layers-chevron" aria-hidden="true">⌄</span>
              </button>
              <div className="layers-popover" role="group" aria-label="Overlay layers">
                <div className="layers-popover-heading">
                  <span>Layers</span>
                  <span>{activeLayerLabels.length} active</span>
                </div>
                <div className="sidebar-layer-controls">
                  <section className="layer-control-section" aria-label="Common weather layers">
                    <p className="layer-control-section-title">Common</p>
                    {commonOptional.map((layer) => (
                      <div className="field-select" key={layer.id}>
                        <input
                          aria-label={layerControlLabel(layer.id)}
                          type="checkbox"
                          data-layer-id={layer.id}
                          id={`layer-${product.id}-${layer.id}`}
                          checked={isLayerEnabled(layer)}
                          onChange={(event) => toggleOptionalLayer(layer, event.target.checked)}
                        />
                        <label htmlFor={`layer-${product.id}-${layer.id}`}>
                          {layerControlLabel(layer.id)}
                        </label>
                      </div>
                    ))}
                  </section>
                  {regionalSatelliteOptional.length > 0 && (
                    <section className="layer-control-section" aria-label="Additional BC satellite products">
                      <p className="layer-control-section-title">Additional BC satellite</p>
                      {regionalSatelliteOptional.map((layer) => (
                        <div className="field-select" key={layer.id}>
                          <input
                            aria-label={layerControlLabel(layer.id)}
                            type="checkbox"
                            data-layer-id={layer.id}
                            id={`layer-${product.id}-${layer.id}`}
                            checked={isLayerEnabled(layer)}
                            onChange={(event) => toggleOptionalLayer(layer, event.target.checked)}
                          />
                          <label htmlFor={`layer-${product.id}-${layer.id}`}>
                            {layerControlLabel(layer.id)}
                          </label>
                        </div>
                      ))}
                    </section>
                  )}
                </div>
              </div>
            </div>
          )}
          <div className="legend-content">
            <h2 className="legend-title">Legend</h2>
            {visibleLegends.map((legendId) => {
              const legend = catalog.legends[legendId];
              if (!legend) return null;
              if (legend.kind === "lightning-age") {
                return <LightningLegend hourly={effectiveRangeHours > 24} key={legendId} />;
              }
              if (legend.kind === "model-hgt500") return <ModelContourLegend kind="hgt500" key={legendId} />;
              if (legend.kind === "model-mslp") return <ModelContourLegend kind="mslp" key={legendId} />;
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
              if (legend.kind === "transmission-lines") return <TransmissionLegend key={legendId} />;
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
          </div>
          <button className="sources-button" type="button" onClick={() => setSourcesOpen(true)}>
            Sources
          </button>
        </aside>
      </section>

      {sourcesOpen && (
        <>
          <button className="sources-backdrop" type="button" aria-label="Close sources" onClick={() => setSourcesOpen(false)} />
          <aside className="sources-drawer" role="dialog" aria-modal="true" aria-labelledby="sources-title">
            <div className="sources-header">
              <div>
                <p className="drawer-eyebrow">Current display</p>
                <h2 id="sources-title">Sources &amp; notes</h2>
              </div>
              <button className="drawer-close" type="button" aria-label="Close sources" onClick={() => setSourcesOpen(false)}>×</button>
            </div>
            <h3>{product.title}</h3>
            <p>{product.description}</p>
            <h3>Data feeds</h3>
            <ul className="source-list">
              {Object.entries(catalog.sources ?? {}).map(([name, url]) => (
                <li key={name}>
                  <a href={url} target="_blank" rel="noreferrer">{name}</a>
                  <span>{SOURCE_SUMMARIES[name] ?? "Public operational data source."}</span>
                </li>
              ))}
            </ul>
            {product.notes.length > 0 && (
              <>
                <h3>Display notes</h3>
                <ul className="drawer-notes">
                  {product.notes.map((note) => <li key={note}>{note}</li>)}
                </ul>
              </>
            )}
          </aside>
        </>
      )}
    </main>
  );
}
