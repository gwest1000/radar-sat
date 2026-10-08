export type Webcam = {
  id: string;
  name: string;
  provider: string;
  credit: string;
  kind: string;
  links: { label: string; url: string }[];
  previews: { label: string; url: string }[];
};
export type WebcamPoint = { id: string; x: number; y: number };
export type WebcamCatalog = {
  schemaVersion: number;
  cameras: Webcam[];
  domains: Record<string, WebcamPoint[]>;
};
export type WebcamGroup = { id: string; x: number; y: number; cameras: Webcam[] };

// Screen-space binning runs only on region/size changes, never per loop frame.
// Every camera remains accessible in its group's dropdown.
export function groupWebcams(
  catalog: WebcamCatalog, domain: string,
  viewport: { left: number; top: number; width: number; height: number },
  width: number, height: number,
): WebcamGroup[] {
  const byId = new Map(catalog.cameras.map((camera) => [camera.id, camera]));
  const groups = new Map<string, WebcamGroup>();
  for (const point of catalog.domains[domain] ?? []) {
    const camera = byId.get(point.id);
    if (!camera?.links.length) continue;
    const x = (point.x - viewport.left) / viewport.width;
    const y = (point.y - viewport.top) / viewport.height;
    if (x < 0 || y < 0 || x > 1 || y > 1) continue;
    const key = `${Math.floor(x * width / 28)}:${Math.floor(y * height / 28)}`;
    const group = groups.get(key);
    if (group) {
      const count = group.cameras.length;
      group.x = (group.x * count + x) / (count + 1);
      group.y = (group.y * count + y) / (count + 1);
      group.cameras.push(camera);
    } else groups.set(key, { id: key, x, y, cameras: [camera] });
  }
  // Mountain/aviation cameras are the most useful initial view in mixed groups.
  for (const group of groups.values()) group.cameras.sort((a, b) =>
    Number(!a.previews?.length) - Number(!b.previews?.length)
    || Number(a.provider === "DriveBC") - Number(b.provider === "DriveBC") || a.name.localeCompare(b.name));
  return [...groups.values()];
}

export function webcamPreviewUrl(camera: Webcam, index: number, now: number): string | undefined {
  const raw = camera.previews[index]?.url;
  if (!raw) return undefined;
  const url = new URL(raw);
  if (url.protocol !== "https:") return undefined;
  // Only add cache-busting to the known public DriveBC endpoint. Arbitrary
  // provider URLs can contain signatures/query tokens that must not be changed.
  if (camera.provider === "DriveBC") url.searchParams.set("t", String(Math.floor(now / 60_000)));
  return url.href;
}
