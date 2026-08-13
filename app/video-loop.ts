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

export type VideoMedia = {
  path: string;
  mimeType: string;
  codec: string;
  width: number;
  height: number;
  contentHeight?: number;
  byteLength: number;
  sha256: string;
  segments?: Array<{
    path: string;
    byteLength: number;
    sha256: string;
    durationSeconds: number;
    firstFrame: number;
    lastFrame: number;
  }>;
};

export type VideoDefaultComposite = {
  id: string;
  layerIds: string[];
  mediaViewport: Record<string, number>;
  media: VideoMedia;
};

export type VideoLoopManifest = {
  schemaVersion: 1;
  generation: string;
  generatedAt: string;
  productId: string;
  layerId: string;
  track: "live" | "archive";
  transport: "progressive-mp4" | "hls-ts";
  cadenceMinutes: number;
  width: number;
  height: number;
  viewport?: Record<string, number>;
  mediaViewport?: Record<string, number>;
  media: VideoMedia;
  defaultComposite?: VideoDefaultComposite;
  frames: VideoManifestFrame[];
  proxies: Record<string, VideoProxy>;
  staticOverlay?: VideoProxy;
};

const MAX_MANIFEST_CACHE_ENTRIES = 8;
const manifestCache = new Map<string, Promise<VideoLoopManifest>>();

function finitePositive(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function validViewport(value: unknown): value is Record<string, number> {
  if (!value || typeof value !== "object") return false;
  const viewport = value as Record<string, unknown>;
  return typeof viewport.left === "number"
    && Number.isFinite(viewport.left)
    && typeof viewport.top === "number"
    && Number.isFinite(viewport.top)
    && finitePositive(viewport.width)
    && finitePositive(viewport.height);
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

function validMedia(value: unknown, transport: unknown): value is VideoMedia {
  if (!value || typeof value !== "object") return false;
  const media = value as Partial<VideoMedia>;
  if (
    typeof media.path !== "string"
    || !media.path
    || typeof media.mimeType !== "string"
    || (
      !media.mimeType.startsWith("video/mp4")
      && media.mimeType !== "application/vnd.apple.mpegurl"
    )
    || typeof media.codec !== "string"
    || !media.codec
    || !finitePositive(media.width)
    || !finitePositive(media.height)
    || (
      media.contentHeight !== undefined
      && (!finitePositive(media.contentHeight) || media.contentHeight > media.height)
    )
    || typeof media.byteLength !== "number"
    || !Number.isFinite(media.byteLength)
    || media.byteLength < 0
    || typeof media.sha256 !== "string"
    || !media.sha256
  ) return false;
  if (transport !== "hls-ts") return true;
  return Array.isArray(media.segments)
    && media.segments.length > 0
    && media.segments.every((segment) => (
      Boolean(segment)
      && typeof segment.path === "string"
      && Boolean(segment.path)
      && finitePositive(segment.byteLength)
      && typeof segment.sha256 === "string"
      && Boolean(segment.sha256)
      && finitePositive(segment.durationSeconds)
      && Number.isInteger(segment.firstFrame)
      && Number.isInteger(segment.lastFrame)
    ));
}

function validDefaultComposite(
  value: unknown,
  manifest: Partial<VideoLoopManifest>,
): value is VideoDefaultComposite {
  if (!value || typeof value !== "object") return false;
  const composite = value as Partial<VideoDefaultComposite>;
  const layerIds = composite.layerIds;
  const contentHeight = composite.media?.contentHeight ?? composite.media?.height;
  const viewport = manifest.viewport ?? { left: 0, top: 0, width: 1, height: 1 };
  const mediaViewport = composite.mediaViewport;
  return typeof composite.id === "string"
    && composite.id.length > 0
    && Array.isArray(layerIds)
    && layerIds.length > 0
    && layerIds.every((id) => typeof id === "string" && id.length > 0)
    && new Set(layerIds).size === layerIds.length
    && validViewport(mediaViewport)
    && mediaViewport.left === viewport.left
    && mediaViewport.top === viewport.top
    && mediaViewport.width === viewport.width
    && mediaViewport.height === viewport.height
    && validMedia(composite.media, manifest.transport)
    && composite.media.width === manifest.width
    && contentHeight === manifest.height;
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
    || !["live", "archive"].includes(String(manifest.track))
    || !["progressive-mp4", "hls-ts"].includes(String(manifest.transport))
    || !finitePositive(manifest.cadenceMinutes)
    || !finitePositive(manifest.width)
    || !finitePositive(manifest.height)
    || (manifest.viewport !== undefined && !validViewport(manifest.viewport))
    || (manifest.mediaViewport !== undefined && !validViewport(manifest.mediaViewport))
    || !validMedia(manifest.media, manifest.transport)
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
  const parsed = manifest as VideoLoopManifest;
  // The composite is an optional fast path. A malformed or partially
  // published preset must never take the healthy satellite-video + proxy
  // compositor down with it.
  if (
    parsed.defaultComposite !== undefined
    && !validDefaultComposite(parsed.defaultComposite, parsed)
  ) {
    return { ...parsed, defaultComposite: undefined };
  }
  return parsed;
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
  const native = video.canPlayType(mimeType) !== "";
  const mediaSource = mimeType === "application/vnd.apple.mpegurl"
    && typeof MediaSource !== "undefined"
    && MediaSource.isTypeSupported('video/mp4; codecs="avc1.640028"');
  return typeof video.requestVideoFrameCallback === "function"
    && (native || mediaSource);
}
