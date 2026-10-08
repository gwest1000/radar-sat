"use client";

import { memo, useEffect, useMemo, useRef, useState } from "react";
import { groupWebcams, webcamPreviewUrl, WebcamCatalog } from "./webcam-data";

export const WebcamOverlay = memo(function WebcamOverlay({ domain, viewport, visible }: {
  domain: string;
  viewport: { left: number; top: number; width: number; height: number };
  visible: boolean;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [catalog, setCatalog] = useState<WebcamCatalog | null>(null);
  const [error, setError] = useState("");
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [active, setActive] = useState<string | null>(null);
  const [cameraIndex, setCameraIndex] = useState(0);
  const [viewIndex, setViewIndex] = useState(0);
  const [openedAt, setOpenedAt] = useState(0);
  const [imageFailed, setImageFailed] = useState(false);
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const touch = useRef(false);
  const cancelClose = () => clearTimeout(closeTimer.current);
  const closeSoon = () => {
    clearTimeout(hoverTimer.current);
    closeTimer.current = setTimeout(() => setActive(null), 220);
  };
  const open = (id: string) => {
    cancelClose();
    clearTimeout(hoverTimer.current);
    setActive(id); setCameraIndex(0); setViewIndex(0);
    setOpenedAt(Date.now()); setImageFailed(false);
  };
  useEffect(() => {
    if (!visible || catalog) return;
    const controller = new AbortController();
    fetch("webcams.json", { signal: controller.signal }).then(async (response) => {
      if (!response.ok) throw new Error("Webcam catalogue unavailable");
      const data = await response.json() as WebcamCatalog;
      if (data.schemaVersion !== 1 || !Array.isArray(data.cameras) || !data.domains) throw new Error("Invalid webcam catalogue");
      setCatalog(data);
    }).catch((reason) => { if (!controller.signal.aborted) setError(String(reason.message)); });
    return () => controller.abort();
  }, [catalog, visible]);
  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const observer = new ResizeObserver(([entry]) => setSize({
      width: Math.round(entry.contentRect.width), height: Math.round(entry.contentRect.height),
    }));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => () => { clearTimeout(hoverTimer.current); clearTimeout(closeTimer.current); }, []);
  const groups = useMemo(() => catalog && size.width
    ? groupWebcams(catalog, domain, viewport, size.width, size.height) : [], [catalog, domain, viewport, size]);
  const group = groups.find((item) => item.id === active);
  const camera = group?.cameras[cameraIndex];
  const imageUrl = camera ? webcamPreviewUrl(camera, viewIndex, openedAt) : undefined;
  const panelWidth = Math.min(360, Math.max(0, size.width - 12));
  const panelLeft = group ? Math.max(6, Math.min(size.width - panelWidth - 6, group.x * size.width + 18)) : 6;
  const panelTop = group ? Math.max(6, Math.min(size.height - 390, group.y * size.height - 20)) : 6;
  return <div ref={host} className="webcam-overlay" aria-label="Current webcams">
    {error && <p className="webcam-message">{error}. Toggle webcams off/on to retry.</p>}
    {!catalog && !error && <p className="webcam-message">Loading camera locations…</p>}
    {catalog && !groups.length && size.width > 0 && <p className="webcam-message">No listed webcams in this view.</p>}
    {groups.map((item) => <a key={item.id} className="webcam-marker"
      href={item.cameras[0].links[0].url} target="_blank" rel="noopener noreferrer"
      style={{ left: `${item.x * 100}%`, top: `${item.y * 100}%` }}
      aria-label={`${item.cameras[0].name}${item.cameras.length > 1 ? `; ${item.cameras.length} nearby webcams` : ""}`}
      onPointerEnter={(event) => {
        if (event.pointerType !== "mouse") return;
        cancelClose(); clearTimeout(hoverTimer.current);
        hoverTimer.current = setTimeout(() => open(item.id), 160);
      }} onPointerLeave={closeSoon} onFocus={(event) => {
        if (event.currentTarget.matches(":focus-visible")) open(item.id);
      }} onBlur={closeSoon}
      onPointerDown={(event) => { touch.current = event.pointerType !== "mouse"; }}
      onClick={(event) => {
        if (touch.current && active !== item.id) { event.preventDefault(); open(item.id); }
      }}>
      <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 7h4l2-3h6l2 3h4v13H3Z" /><circle cx="12" cy="13" r="4" /></svg>
      {item.cameras.length > 1 && <span>{item.cameras.length}</span>}
    </a>)}
    {visible && group && camera && <section className="webcam-preview" aria-label="Webcam preview"
      style={{ width: panelWidth, left: panelLeft, top: panelTop, maxHeight: Math.max(80, size.height - 12) }}
      onPointerEnter={cancelClose} onPointerLeave={closeSoon} onFocus={cancelClose}
      onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) closeSoon(); }}
      onKeyDown={(event) => { if (event.key === "Escape") { clearTimeout(hoverTimer.current); setActive(null); } }}>
      <button className="webcam-close" type="button" aria-label="Close webcam preview" onClick={() => setActive(null)}>×</button>
      <strong>{camera.name}</strong>
      <p>Latest webcam view — not the loop time</p>
      {group.cameras.length > 1 && <label>Nearby cameras<select aria-label="Nearby cameras" value={cameraIndex} onChange={(event) => {
        setCameraIndex(Number(event.target.value)); setViewIndex(0); setImageFailed(false); setOpenedAt(Date.now());
      }}>{group.cameras.map((item, index) => <option value={index} key={item.id}>{item.name}</option>)}</select></label>}
      {camera.previews.length > 1 && <label>Camera view<select aria-label="Camera view" value={viewIndex} onChange={(event) => {
        setViewIndex(Number(event.target.value)); setImageFailed(false);
      }}>{camera.previews.map((view, index) => <option value={index} key={index}>{view.label}</option>)}</select></label>}
      {imageUrl && !imageFailed ? <a href={camera.links[0].url} target="_blank" rel="noopener noreferrer">
        {/* Provider-hosted pixels are fetched only for this one open preview. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img key={imageUrl} src={imageUrl} alt={`${camera.name}: ${camera.previews[viewIndex].label}`} referrerPolicy="no-referrer" onError={() => setImageFailed(true)} />
      </a> : <p>{imageFailed ? "The provider’s preview could not be loaded." : "This provider has no direct image preview."} Open its webpage below.</p>}
      {camera.links.map((link) => <a className="webcam-source-link" href={link.url} key={link.url} target="_blank" rel="noopener noreferrer">{link.label} ↗</a>)}
      <small>{camera.credit}</small>
      <small>Images may be delayed or offline; check the camera’s own timestamp.</small>
    </section>}
  </div>;
});
