"use client";

import { memo, useEffect, useId, useState } from "react";
import { dailyTrackPoints, pacificClock, pacificTrackClock } from "./pacific-time";
import { stormPresentInViewport } from "./storm-track-policy";

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
  const [asOf, setAsOf] = useState<number | null>(null);
  const arrowId = `storm-arrow-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    const refresh = () => fetch("storm-tracks.json", { signal: controller.signal, cache: "no-cache" }).then(async (response) => {
      if (!response.ok) throw new Error("Storm-track trial data unavailable");
      const data = await response.json() as Catalog;
      if (data.schemaVersion !== 1 || data.model !== "RDPS" || !data.domains) throw new Error("Invalid storm tracks");
      setCatalog(data);
      setAsOf(Date.now());
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
  const tracks = (catalog?.domains[domain] ?? []).filter((track) =>
    stormPresentInViewport(track.points, asOf ?? NaN, viewport));
  return <div className="storm-track-overlay" aria-label="Experimental RDPS storm tracks">
    <p className="storm-track-key" title={error || (catalog
      ? `RDPS ${clock(catalog.referenceTime)}${stale ? " — STALE" : ""} · BC + ${catalog.selection.bufferKm} km${!tracks.length ? " · no current systems in this view" : ""} · latest predicted paths for storms currently in this view; independent of loop time · trial numbers reset each run`
      : "Loading RDPS storm tracks…")}>Beta: RDPS storm tracks
      {(error || stale) && <span role="status"> · {error ? "unavailable" : "stale"}</span>}
    </p>
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="RDPS cyclone paths">
      <defs>
        <marker id={arrowId} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M 1 1 L 9 5 L 1 9 Z" fill="#75efef" stroke="#000" strokeWidth="1.3" />
        </marker>
      </defs>
      {(catalog?.boundaries?.[domain] ?? []).map((ring, index) =>
        <polyline key={`boundary-${index}`} className="storm-selection-boundary"
          points={ring.map(([x, y]) => `${(x - viewport.left) / viewport.width * 100},${(y - viewport.top) / viewport.height * 100}`).join(" ")}
          aria-label={`BC plus ${catalog!.selection.bufferKm} kilometre storm-selection boundary`} />)}
      {tracks.map((track) => {
        const points = track.points.map(xy);
        const label = points.find((p) => p.x > 2 && p.x < 96 && p.y > 5 && p.y < 96);
        if (!label) return null;
        const endSegment = points.findLastIndex((p, index) => index > 0 && (
          Math.abs(p.x - points[index - 1].x) + Math.abs(p.y - points[index - 1].y) > 0.00001
          && Math.abs(p.x - points[index - 1].x) <= 50
        ));
        return <g key={track.id}>
          {points.slice(1).map((p, index) => {
            const previous = points[index];
            // Avoid drawing a spurious world-spanning segment at the map seam.
            if (Math.abs(p.x - previous.x) > 50) return null;
            return <line key={p.time} x1={previous.x} y1={previous.y} x2={p.x} y2={p.y}
              markerEnd={index + 1 === endSegment ? `url(#${arrowId})` : undefined}
              strokeDasharray={Date.parse(p.time) > Date.parse(catalog!.generatedAt) ? "5 4" : undefined} />;
          })}
          {points.filter((p) => p.x >= 0 && p.x <= 100 && p.y >= 0 && p.y <= 100).map((p) =>
            <circle key={p.time} cx={p.x} cy={p.y} r="0.24">
              <title>{`Track ${track.number} · ${pacificClock(p.time, true)} · ${Math.round(p.pressure)} hPa · RDPS`}</title>
            </circle>)}
          {dailyTrackPoints(points).filter((p) => p.x > 1 && p.x < 96 && p.y > 2 && p.y < 97).map((p, index) =>
            <text key={`day-${p.time}`} className="storm-daily-label"
              textAnchor={p.x > 65 ? "end" : "start"}
              x={p.x + (p.x > 65 ? -0.65 : 0.65)} y={p.y + (index % 2 ? -1.9 : 1.65)}>
              {pacificTrackClock(p.time)}
            </text>)}
          <text x={label.x + 0.65} y={label.y - 0.7}>
            <title>{track.id}</title>{`#${track.number}`}
          </text>
        </g>;
      })}
    </svg>
  </div>;
});
