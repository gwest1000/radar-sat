"use client";

import { CSSProperties, useCallback, useEffect, useMemo, useState } from "react";

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
  mediumConfidencePixels?: number;
  highConfidencePixels?: number;
  mappedFlashCount?: number;
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
const PLAYBACK_SPEEDS = [0.5, 0.75, 1, 1.5, 2, 3, 4, 5];
const NEWEST_FRAME = Number.MAX_SAFE_INTEGER;
const FULL_VIEWPORT: Viewport = { left: 0, top: 0, width: 1, height: 1 };

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
  return ["raw-visible", "raw-ir", "natural", "ir", "daynight", "convective", "radar-rain", "ptype", "lightning-trail", "hotspots"]
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
  if (["daynight", "ir", "natural", "convective", "snowfog", "raw-visible", "raw-ir"].includes(layerId)) return "SAT";
  return layerId.toUpperCase();
}

function sourceLabel(layerId: string): string | null {
  if (layerId.includes("coverage")) return null;
  const label = layerLabel(layerId);
  return ["SAT", "RADAR", "PTYPE", "LTG", "FIRE", "SMOKE"].includes(label) ? label : null;
}

function layerControlLabel(layerId: string): string {
  if (layerId === "natural") return "Visible Satellite";
  if (layerId === "ir") return "Infra-Red Satellite";
  if (layerId === "daynight") return "VisIR Blend";
  if (layerId === "convective") return "Convective Satellite";
  if (layerId === "raw-visible") return "Raw True Colour";
  if (layerId === "raw-ir") return "Raw Enhanced IR";
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
  if (layerId === "hotspots") return "Wildfire Hotspots (24 h)";
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
  if (layerId === "raw-visible" || layerId === "raw-ir") return [90, 150];
  // The source valid time typically trails receipt by roughly 20–40 minutes;
  // use source-aware limits so normal ECCC publication latency is not reported
  // as a local ingest outage.
  return [45, 75];
}

function LightningLegend() {
  const rows = [
    ["0–10 min", "#ff42d6"],
    ["10–20 min", "#c26ad5"],
    ["20–30 min", "#807ea4"],
  ];
  return (
    <div className="lightning-legend" aria-label="Lightning age legend">
      {rows.map(([label, colour]) => (
        <div className="lightning-key-row" key={label}>
          <span className="lightning-symbol" style={{ background: colour }} />
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
      <p>
        {frame?.availability === "unavailable"
          ? "Unavailable for this scene"
          : "Daylight, sufficiently clear sky only; absence is not proof of clear air"}
      </p>
    </div>
  );
}

function HotspotLegend({ frame }: { frame?: Frame }) {
  const rows = [
    ["0–6 h", "#ffe55c"],
    ["6–12 h", "#ff941f"],
    ["12–24 h", "#d94b3d"],
  ];
  return (
    <div className="hotspot-legend" aria-label="Wildfire hotspot detection age legend">
      {rows.map(([label, colour]) => (
        <div className="hotspot-key-row" key={label}>
          <span className="hotspot-symbol" style={{ background: colour }} />
          <span>{label}</span>
        </div>
      ))}
      <p>
        {typeof frame?.detectionCount === "number"
          ? `${frame.detectionCount} mapped detections in this 24 h snapshot`
          : "Satellite thermal detections"}
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
  const [speedIndex, setSpeedIndex] = useState(2);
  const [rangeHours, setRangeHours] = useState(3);
  const [optionalLayers, setOptionalLayers] = useState<Record<string, boolean>>({});
  const [freshnessClock, setFreshnessClock] = useState<number | null>(null);

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
              const preferred = availableProducts.find((item) => item.id === "bc-large-overlay") ?? availableProducts[0];
              setProductId((current) => availableProducts.some((item) => item.id === current) ? current : preferred.id);
              setError("");
              if (!initialized) {
                setRangeHours(preferred.defaultHours);
                setFrameIndex(NEWEST_FRAME);
                setPlaying(!window.matchMedia("(prefers-reduced-motion: reduce)").matches);
                initialized = true;
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
    if (!isAnimating || !domain || !product || !catalogBase) return;
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
    let cancelled = false;
    let timer: number | undefined;
    const loads = nextUrls.map((url) => new Promise<void>((resolve) => {
      const image = new Image();
      image.onload = () => resolve();
      image.onerror = () => resolve();
      image.src = url;
      if (image.complete) resolve();
    }));

    void Promise.all(loads).then(() => {
      if (cancelled) return;
      const finalFrame = currentFrameIndex === anchorFrames.length - 1;
      const delay = finalFrame ? 1200 / speed : 300 / speed;
      timer = window.setTimeout(() => advance(1), delay);
    });

    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [
    advance,
    anchorFrames,
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

  const anchor = anchorFrames[currentFrameIndex];
  const isLayerEnabled = (recipe: ProductLayer) =>
    isProductLayerEnabled(recipe, optionalLayers, product.layers);
  const composedLayers = anchor
    ? composeLayers(product, domain, anchor, catalogBase, optionalLayers)
    : [];
  const sourceTimes = composedLayers
    .filter((item): item is typeof item & { frame: Frame } => "frame" in item && Boolean(item.frame))
    .map((item) => ({ label: sourceLabel(item.id), validTime: actualSourceTime(item.id, item.frame) }))
    .filter((item): item is { label: string; validTime: string } => Boolean(item.label))
    .filter((item, index, all) => all.findIndex((candidate) => candidate.label === item.label && candidate.validTime === item.validTime) === index)
    .map((item) => `${item.label} ${shortClock(item.validTime)}`)
    .join(" · ");
  const composedLayerIds = new Set(composedLayers.map((layer) => layer.id));
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
            "--map-cap-width": `calc(${(mapAspect * 100).toFixed(6)}vh - ${(mapAspect * 330).toFixed(3)}px)`,
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
                    && ["natural", "ir", "daynight", "convective", "raw-visible", "raw-ir"].includes(layer.id)
                    && (composedLayerIds.has("radar-rain") || composedLayerIds.has("ptype"))
                    ? "saturate(0.52) brightness(0.78) contrast(1.06)"
                    : undefined,
                }}
              />
            ))}
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
              const hotspotFrame = composedLayers.find((layer) => layer.id === "hotspots")?.frame;
              return <HotspotLegend frame={hotspotFrame} key={legendId} />;
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
