export type VideoProfilePointer = {
  generation: string;
  manifestPath: string;
};

export type VideoProxy = {
  path: string;
  width: number;
  height: number;
  byteLength: number;
};

export type VideoProxyLayerSelection = {
  id: string;
  renderId: string;
  sourceKey: string;
  sourceValidTime: string | null;
};

export type VideoManifestFrame = {
  index: number;
  validTime: string;
  sourceValidTime: string;
  sourceTimes?: Record<string, string>;
  encodedSourceLayer: string;
  sourcePath: string;
  sourceFetchedAt: string;
  ptsSeconds: number;
  durationSeconds: number;
  proxyLayers: VideoProxyLayerSelection[];
};

export type VideoLoopManifest = {
  schemaVersion: 1;
  generation: string;
  generatedAt: string;
  productId: string;
  layerId: string;
  track: "live";
  transport: "progressive-mp4";
  cadenceMinutes: number;
  width: number;
  height: number;
  viewport?: Record<string, number>;
  media: {
    path: string;
    mimeType: string;
    codec: string;
    width: number;
    height: number;
    byteLength: number;
    sha256: string;
  };
  frames: VideoManifestFrame[];
  proxies: Record<string, VideoProxy>;
  staticOverlay?: VideoProxy;
};

const MAX_MANIFEST_CACHE_ENTRIES = 8;
const manifestCache = new Map<string, Promise<VideoLoopManifest>>();

function finitePositive(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function validProxy(value: unknown): value is VideoProxy {
  if (!value || typeof value !== "object") return false;
  const proxy = value as Partial<VideoProxy>;
  return typeof proxy.path === "string"
    && proxy.path.length > 0
    && finitePositive(proxy.width)
    && finitePositive(proxy.height)
    && typeof proxy.byteLength === "number"
    && Number.isFinite(proxy.byteLength)
    && proxy.byteLength >= 0;
}

function validFrame(value: unknown): value is VideoManifestFrame {
  if (!value || typeof value !== "object") return false;
  const frame = value as Partial<VideoManifestFrame>;
  return Number.isInteger(frame.index)
    && typeof frame.validTime === "string"
    && Number.isFinite(Date.parse(frame.validTime))
    && typeof frame.sourceValidTime === "string"
    && Number.isFinite(Date.parse(frame.sourceValidTime))
    && typeof frame.encodedSourceLayer === "string"
    && typeof frame.sourcePath === "string"
    && typeof frame.sourceFetchedAt === "string"
    && typeof frame.ptsSeconds === "number"
    && Number.isFinite(frame.ptsSeconds)
    && frame.ptsSeconds >= 0
    && finitePositive(frame.durationSeconds)
    && Array.isArray(frame.proxyLayers)
    && frame.proxyLayers.every((value) => {
      if (!value || typeof value !== "object") return false;
      const layer = value as Partial<VideoProxyLayerSelection>;
      return typeof layer.id === "string"
        && layer.id.length > 0
        && typeof layer.renderId === "string"
        && layer.renderId.length > 0
        && typeof layer.sourceKey === "string"
        && layer.sourceKey.length > 0
        && (
          layer.sourceValidTime === null
          || (
            typeof layer.sourceValidTime === "string"
            && Number.isFinite(Date.parse(layer.sourceValidTime))
          )
        );
    });
}

export function parseVideoLoopManifest(value: unknown): VideoLoopManifest {
  if (!value || typeof value !== "object") throw new Error("Video manifest is not an object.");
  const manifest = value as Partial<VideoLoopManifest>;
  if (
    manifest.schemaVersion !== 1
    || typeof manifest.generation !== "string"
    || typeof manifest.generatedAt !== "string"
    || typeof manifest.productId !== "string"
    || typeof manifest.layerId !== "string"
    || manifest.track !== "live"
    || manifest.transport !== "progressive-mp4"
    || !finitePositive(manifest.cadenceMinutes)
    || !finitePositive(manifest.width)
    || !finitePositive(manifest.height)
    || !manifest.media
    || typeof manifest.media.path !== "string"
    || typeof manifest.media.mimeType !== "string"
    || !manifest.media.mimeType.startsWith("video/mp4")
    || typeof manifest.media.codec !== "string"
    || !finitePositive(manifest.media.width)
    || !finitePositive(manifest.media.height)
    || typeof manifest.media.byteLength !== "number"
    || manifest.media.byteLength < 0
    || typeof manifest.media.sha256 !== "string"
    || !Array.isArray(manifest.frames)
    || manifest.frames.length < 2
    || !manifest.frames.every(validFrame)
    || !manifest.proxies
    || typeof manifest.proxies !== "object"
    || !Object.values(manifest.proxies).every(validProxy)
    || (manifest.staticOverlay !== undefined && !validProxy(manifest.staticOverlay))
  ) {
    throw new Error("Video manifest has an unsupported schema.");
  }

  for (let index = 0; index < manifest.frames.length; index += 1) {
    const current = manifest.frames[index];
    if (current.index !== index) throw new Error("Video manifest frame indexes are not contiguous.");
    if (current.proxyLayers.some((layer) => !manifest.proxies?.[layer.sourceKey])) {
      throw new Error("Video manifest frame references an unavailable proxy.");
    }
    if (index === 0) continue;
    const previous = manifest.frames[index - 1];
    if (
      Date.parse(current.validTime) <= Date.parse(previous.validTime)
      || current.ptsSeconds <= previous.ptsSeconds
    ) {
      throw new Error("Video manifest times are not strictly increasing.");
    }
  }
  return manifest as VideoLoopManifest;
}

export function loadVideoLoopManifest(url: string): Promise<VideoLoopManifest> {
  const existing = manifestCache.get(url);
  if (existing) {
    manifestCache.delete(url);
    manifestCache.set(url, existing);
    return existing;
  }
  const request = fetch(url, { cache: "force-cache", mode: "cors" })
    .then(async (response) => {
      if (!response.ok) throw new Error(`Video manifest returned ${response.status}.`);
      return parseVideoLoopManifest(await response.json());
    })
    .catch((error) => {
      if (manifestCache.get(url) === request) manifestCache.delete(url);
      throw error;
    });
  manifestCache.set(url, request);
  while (manifestCache.size > MAX_MANIFEST_CACHE_ENTRIES) {
    const oldest = manifestCache.keys().next().value;
    if (typeof oldest !== "string") break;
    manifestCache.delete(oldest);
  }
  return request;
}

export function sourceCacheKey(path: string, revision: string): string {
  const query = new URLSearchParams({ v: revision });
  return `${path}?${query.toString()}`;
}

export function selectVideoFrames(
  manifest: VideoLoopManifest,
  rangeHours: number,
): VideoManifestFrame[] {
  if (!manifest.frames.length) return [];
  const newest = Date.parse(manifest.frames[manifest.frames.length - 1].validTime);
  const start = newest - rangeHours * 3_600_000;
  const selected = manifest.frames.filter((frame) => Date.parse(frame.validTime) >= start);
  return selected.length >= 2 ? selected : manifest.frames.slice(-2);
}

export function videoFrameAtMediaTime(
  frames: VideoManifestFrame[],
  mediaTime: number,
): number {
  let low = 0;
  let high = frames.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    if (frames[middle].ptsSeconds <= mediaTime + 0.0005) low = middle + 1;
    else high = middle - 1;
  }
  return Math.max(0, Math.min(frames.length - 1, high));
}

export function supportsVideoLoop(mimeType: string): boolean {
  if (typeof document === "undefined") return false;
  const video = document.createElement("video");
  return typeof video.requestVideoFrameCallback === "function"
    && video.canPlayType(mimeType) !== "";
}
