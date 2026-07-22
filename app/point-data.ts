export type HazardPointPayload = {
  schemaVersion: number;
  kind?: string;
  type?: string;
  layer?: string;
  domain: string;
  validTime: string;
  coordinateSpace: string | {
    type?: string;
    origin?: string;
    xRange?: number[];
    yRange?: number[];
  };
  points: number[][];
};

const pointFrameCache = new Map<string, Promise<HazardPointPayload>>();

function validPayload(value: unknown): value is HazardPointPayload {
  if (!value || typeof value !== "object") return false;
  const payload = value as Partial<HazardPointPayload>;
  const coordinateSpace = payload.coordinateSpace;
  const normalizedTopLeft = coordinateSpace === "normalized-top-left"
    || (
      typeof coordinateSpace === "object"
      && coordinateSpace !== null
      && coordinateSpace.type === "normalized"
      && coordinateSpace.origin === "top-left"
    );
  return payload.schemaVersion === 1
    && (typeof payload.kind === "string" || payload.type === "point-frame")
    && typeof payload.domain === "string"
    && typeof payload.validTime === "string"
    && normalizedTopLeft
    && Array.isArray(payload.points);
}

export function loadPointFrame(url: string): Promise<HazardPointPayload> {
  const existing = pointFrameCache.get(url);
  if (existing) return existing;

  const request = fetch(url, { cache: "force-cache" })
    .then(async (response) => {
      if (!response.ok) throw new Error(`Point frame returned ${response.status}`);
      const payload: unknown = await response.json();
      if (!validPayload(payload)) throw new Error("Point frame has an unsupported schema");
      return payload;
    })
    .catch((error) => {
      pointFrameCache.delete(url);
      throw error;
    });
  pointFrameCache.set(url, request);
  return request;
}

export function preloadPointFrame(url: string): void {
  void loadPointFrame(url).catch(() => undefined);
}
