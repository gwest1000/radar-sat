"use client";

import { useEffect, useRef, useState } from "react";

export function FrameExportButton({ disabled, capture }: {
  disabled: boolean;
  capture: () => { image: Promise<Blob>; filename: string };
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const objectUrl = useRef("");
  const mounted = useRef(true);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ url: string; filename: string } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; if (objectUrl.current) URL.revokeObjectURL(objectUrl.current); };
  }, []);

  async function exportFrame() {
    if (busy) return;
    setBusy(true); setError(""); setResult(null);
    dialog.current?.showModal();
    try {
      const { image, filename } = capture();
      const blob = await image;
      if (!mounted.current) return;
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
      objectUrl.current = URL.createObjectURL(blob);
      setResult({ url: objectUrl.current, filename });
    } catch (reason) {
      if (mounted.current) setError(reason instanceof DOMException && reason.name === "SecurityError"
        ? "A map source does not allow image export in this browser. Please try another view."
        : reason instanceof Error ? reason.message : "This frame could not be exported. Please try again.");
    } finally { if (mounted.current) setBusy(false); }
  }

  return <>
    <button type="button" className="frame-export-button" disabled={disabled || busy} onClick={() => void exportFrame()}
      title="Pause and export the displayed map, timestamp and layers as a PNG image">Export frame</button>
    <dialog ref={dialog} className="frame-export-dialog" aria-labelledby="frame-export-title">
      <div className="frame-export-heading">
        <h2 id="frame-export-title">Export frame</h2>
        <button type="button" autoFocus onClick={() => dialog.current?.close()} aria-label="Close frame export">Close</button>
      </div>
      {busy && <p role="status">Creating the image…</p>}
      {error && <p role="alert">{error}</p>}
      {result && <>
        <div className="frame-export-actions">
          <a href={result.url} download={result.filename}>Save PNG</a>
          <a href={result.url} target="_blank" rel="noopener">Open image in new tab</a>
        </div>
        <p>Right-click the image to save it, or touch and hold on iPad. The loop is paused at the exported frame.</p>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={result.url} alt={`Exported weather map: ${result.filename}`} />
      </>}
    </dialog>
  </>;
}
