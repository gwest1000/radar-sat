"use client";

import { CSSProperties, useCallback, useEffect, useMemo, useState } from "react";

type Frame = {
  validTime: string;
  path: string;
  source: string;
  sourceLayer: string;
  fetchedAt: string;
  sourceTimes?: Record<string, string>;
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
const NEWEST_FRAME = Number.MAX_SAFE_INTEGER;

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
): boolean {
  if (!recipe.optional) return true;
  return optionalLayers[recipe.id] ?? recipe.defaultEnabled ?? true;
}

function composeLayers(
  product: Product,
  domain: Domain,
  anchor: Frame,
  catalogBase: string,
  optionalLayers: Record<string, boolean>,
): ComposedLayer[] {
  return product.layers.flatMap((recipe) => {
    if (!isProductLayerEnabled(recipe, optionalLayers)) return [];
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
    const frame = recipe.id === "lightning-trail"
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
  if (layerId === "lightning-trail" && frame.sourceTimes) {
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

function sourceAgeLabel(minutes: number): string {
  if (!Number.isFinite(minutes)) return "newest source unknown";
  if (minutes < 1) return "newest source <1 min old";
  return `newest source ${Math.round(minutes)} min old`;
}

function layerLabel(layerId: string): string {
  if (layerId.startsWith("radar")) return "RADAR";
  if (layerId.startsWith("lightning")) return "LTG";
  if (layerId === "ptype") return "PTYPE";
  if (layerId === "site-radar") return "RADAR";
  if (["daynight", "ir", "natural", "convective", "snowfog"].includes(layerId)) return "SAT";
  return layerId.toUpperCase();
}

function sourceLabel(layerId: string): string | null {
  if (layerId.includes("coverage")) return null;
  const label = layerLabel(layerId);
  return ["SAT", "RADAR", "PTYPE", "LTG"].includes(label) ? label : null;
}

function layerControlLabel(layerId: string): string {
  if (layerId === "daynight") return "Satellite";
  if (layerId === "radar-rain") return "Rain rate";
  if (layerId === "radar-snow") return "Snow rate";
  if (layerId === "radar-coverage") return "Radar coverage";
  if (layerId === "ptype-coverage") return "Precipitation-type coverage";
  if (layerId === "lightning-trail") return "Age trail";
  if (layerId === "lightning") return "Flash density";
  return layerLabel(layerId);
}

function legendLayerId(legendId: string): string {
  if (legendId === "lightning-age") return "lightning-trail";
  if (legendId === "lightning-density") return "lightning";
  return legendId;
}

function freshnessThresholds(layerId: string): [number, number] {
  if (layerId.startsWith("radar") || layerId === "site-radar") return [12, 20];
  if (layerId === "ptype") return [18, 30];
  if (layerId.startsWith("lightning")) return [22, 35];
  return [25, 40];
}

function LightningLegend() {
  const rows = [
    ["0–10 min", "#ffffff"],
    ["10–20 min", "#00e5ff"],
    ["20–30 min", "#ff9f1c"],
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

export function RadarViewer() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [catalogBase, setCatalogBase] = useState("");
  const [error, setError] = useState("");
  const [productId, setProductId] = useState("bc-operations");
  const [frameIndex, setFrameIndex] = useState(NEWEST_FRAME);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
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
              const preferred = availableProducts.find((item) => item.id === "bc-operations") ?? availableProducts[0];
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
  const anchorFrames = useMemo(() => {
    if (!domain || !product) return [];
    const frames = domain.layers[product.anchorLayer]?.frames ?? [];
    if (!frames.length) return [];
    const newest = Date.parse(frames[frames.length - 1].validTime);
    const cutoff = newest - rangeHours * 60 * 60 * 1000;
    return frames.filter((frame) => Date.parse(frame.validTime) >= cutoff);
  }, [domain, product, rangeHours]);

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
        <h1 className="brand">Radar-Sat</h1>
        <div className="error-panel" role="alert">{error}</div>
      </main>
    );
  }
  if (!catalog || !product || !domain) return <main className="loading-page">Loading observational loops…</main>;

  const anchor = anchorFrames[currentFrameIndex];
  const isLayerEnabled = (recipe: ProductLayer) =>
    isProductLayerEnabled(recipe, optionalLayers);
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
  const newestAnchor = anchorFrames[anchorFrames.length - 1];
  const freshestAge = newestAnchor && freshnessClock !== null
    ? Math.max(0, (freshnessClock - Date.parse(actualSourceTime(product.anchorLayer, newestAnchor))) / 60_000)
    : Infinity;
  const [currentLimit, delayedLimit] = freshnessThresholds(product.anchorLayer);
  const liveState = freshnessClock === null
    ? "Checking"
    : freshestAge <= currentLimit
      ? "Current"
      : freshestAge <= delayedLimit
        ? "Delayed"
      : "Archive";
  const liveSummaryLabel = freshnessClock === null
    ? "Checking data freshness"
    : liveState === "Current"
      ? `Current · ${sourceAgeLabel(freshestAge)}`
      : liveState === "Delayed"
        ? `Data delayed · ${sourceAgeLabel(freshestAge)}`
        : `Not live · ${sourceAgeLabel(freshestAge)}`;
  const selectedArchiveSpan = archiveSpan(anchorFrames);

  return (
    <main className="app-shell">
      <header className="site-header">
        <div>
          <div className="brand-row">
            <h1 className="brand">Radar<span className="brand-mark">–Sat</span></h1>
          </div>
          <p className="tagline">BC observational loops · satellite, radar, precipitation type, and lightning</p>
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
              setPlaying(false);
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
            "--map-aspect": `${domain.width} / ${domain.height}`,
            "--map-cap-width": `calc(${((domain.width / domain.height) * 100).toFixed(6)}vh - ${((domain.width / domain.height) * 300).toFixed(3)}px)`,
          } as CSSProperties}
        >
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
                  opacity: layer.opacity,
                  filter: product.id === "bc-operations"
                    && layer.id === "daynight"
                    && (composedLayerIds.has("radar-rain") || composedLayerIds.has("radar-snow"))
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

          <div className="timeline-panel">
            <div className="transport-row">
              <div className="transport-actions">
                <button className="control-button" type="button" aria-label="Previous frame" disabled={anchorFrames.length < 2} onClick={() => { setPlaying(false); advance(-1); }}>‹</button>
                <button className="control-button primary" type="button" aria-pressed={isAnimating} disabled={anchorFrames.length < 2} onClick={() => setPlaying((value) => !value)}>{isAnimating ? "Pause" : "Play"}</button>
                <button className="control-button" type="button" aria-label="Next frame" disabled={anchorFrames.length < 2} onClick={() => { setPlaying(false); advance(1); }}>›</button>
              </div>
              <div className="timeline-metadata">
                <span className="frame-count">{anchorFrames.length ? `${currentFrameIndex + 1} / ${anchorFrames.length}` : "0 / 0"}</span>
                <span className="archive-span">{selectedArchiveSpan}</span>
              </div>
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
            <div className="range-row">
              <div className="range-actions" role="group" aria-label="Archive range">
                {RANGE_OPTIONS.map((hours) => (
                  <button className="range-button" type="button" aria-pressed={rangeHours === hours} key={hours} onClick={() => { setRangeHours(hours); setFrameIndex(NEWEST_FRAME); }}>
                    {hours === 168 ? "7 d" : `${hours} h`}
                  </button>
                ))}
              </div>
              <div className="range-actions" role="group" aria-label="Layer visibility and playback speed">
                {optional.map((layer) => (
                  <label className="field-select" key={layer.id}>
                    <input
                      type={layer.choiceGroup ? "radio" : "checkbox"}
                      name={layer.choiceGroup ? `${product.id}-${layer.choiceGroup}` : undefined}
                      checked={isLayerEnabled(layer)}
                      onChange={(event) => setOptionalLayers((current) => {
                        if (!layer.choiceGroup) return { ...current, [layer.id]: event.target.checked };
                        const next = { ...current };
                        for (const peer of optional) {
                          if (peer.choiceGroup === layer.choiceGroup) next[peer.id] = peer.id === layer.id;
                        }
                        return next;
                      })}
                    />
                    {layerControlLabel(layer.id)}
                  </label>
                ))}
                <label className="field-select">
                  Speed
                  <select value={speed} onChange={(event) => setSpeed(Number(event.target.value))}>
                    <option value={0.5}>0.5×</option>
                    <option value={1}>1×</option>
                    <option value={2}>2×</option>
                  </select>
                </label>
              </div>
            </div>
          </div>
        </div>

        <aside className="legend-rail" aria-label="Map legends">
          <h2 className="legend-title">Legend</h2>
          {visibleLegends.map((legendId) => {
            const legend = catalog.legends[legendId];
            if (!legend) return null;
            if (legend.kind === "lightning-age") return <LightningLegend key={legendId} />;
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
