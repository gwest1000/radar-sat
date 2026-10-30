"use client";

import { memo, useEffect, useState } from "react";

type Point = { x: number; y: number; time: string; pressure: number };
type Track = { id: string; number: number; points: Point[] };
type Catalog = {
  schemaVersion: number; model: string; referenceTime: string; generatedAt: string;
  domains: Record<string, Track[]>;
  boundaries: Record<string, number[][][]>;
  selection: { bufferKm: number; region: string };
};

export const StormTrackOverlay = memo(function StormTrackOverlay({ domain, viewport, visible }: {
  domain: string;
  viewport: { left: number; top: number; width: number; height: number };
  visible: boolean;
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    const refresh = () => fetch("storm-tracks.json", { signal: controller.signal, cache: "no-cache" }).then(async (response) => {
      if (!response.ok) throw new Error("Storm-track trial data unavailable");
      const data = await response.json() as Catalog;
      if (data.schemaVersion !== 1 || data.model !== "RDPS" || !data.domains) throw new Error("Invalid storm tracks");
      setCatalog(data);
      setError("");
    }).catch((reason) => { if (!controller.signal.aborted) setError(String(reason.message)); });
    void refresh();
    const timer = window.setInterval(refresh, 30 * 60_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [visible]);
  const xy = (p: Point) => ({ ...p,
    x: (p.x - viewport.left) / viewport.width * 100,
    y: (p.y - viewport.top) / viewport.height * 100,
  });
  const clock = (time: string) => new Date(time).toISOString().slice(5, 16).replace("T", " ") + "Z";
  const stale = catalog && Date.now() - Date.parse(catalog.referenceTime) > 12 * 3600_000;
  return <div className="storm-track-overlay" aria-label="Experimental RDPS storm tracks">
    <p className="storm-track-key">{error || (catalog
      ? `RDPS ${clock(catalog.referenceTime)}${stale ? " — STALE" : ""} · BC + ${catalog.selection.bufferKm} km${!catalog.domains[domain]?.length ? " · no qualifying systems" : ""} · latest forecast, independent of loop time · trial numbers reset each run`
      : "Loading RDPS storm tracks…")}</p>
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="RDPS cyclone paths">
      {(catalog?.boundaries?.[domain] ?? []).map((ring, index) =>
        <polyline key={`boundary-${index}`} className="storm-selection-boundary"
          points={ring.map(([x, y]) => `${(x - viewport.left) / viewport.width * 100},${(y - viewport.top) / viewport.height * 100}`).join(" ")}
          aria-label={`BC plus ${catalog!.selection.bufferKm} kilometre storm-selection boundary`} />)}
      {(catalog?.domains[domain] ?? []).map((track) => {
        const points = track.points.map(xy);
        const label = points.find((p) => p.x > 2 && p.x < 96 && p.y > 5 && p.y < 96);
        if (!label) return null;
        return <g key={track.id}>
          {points.slice(1).map((p, index) => {
            const previous = points[index];
            // Avoid drawing a spurious world-spanning segment at the map seam.
            if (Math.abs(p.x - previous.x) > 50) return null;
            return <line key={p.time} x1={previous.x} y1={previous.y} x2={p.x} y2={p.y}
              strokeDasharray={Date.parse(p.time) > Date.parse(catalog!.generatedAt) ? "5 4" : undefined} />;
          })}
          {points.filter((p) => p.x >= 0 && p.x <= 100 && p.y >= 0 && p.y <= 100).map((p) =>
            <circle key={p.time} cx={p.x} cy={p.y} r="0.24">
              <title>{`Track ${track.number} · ${clock(p.time)} · ${Math.round(p.pressure)} hPa · RDPS`}</title>
            </circle>)}
          <text x={label.x + 0.65} y={label.y - 0.7}>
            <title>{track.id}</title>{`#${track.number}`}
          </text>
        </g>;
      })}
    </svg>
  </div>;
});
