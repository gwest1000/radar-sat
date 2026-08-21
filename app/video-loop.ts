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

export type VideoCompositeRendition = {
  id: string;
  media: VideoMedia;
};

export type VideoCompositeRange = {
  hours: number;
  firstFrame: number;
  frameCount: number;
  durationsSeconds: number[];
  boundaryIntervalMultiplier: number;
  renditions: VideoCompositeRendition[];
};

export type VideoCompositePreset = {
  id: string;
  layerIds: string[];
  mediaViewport: Record<string, number>;
  ranges: VideoCompositeRange[];
};

export type VideoLoopManifest = {
  schemaVersion: 1 | 2;
  generation: string;
  generatedAt: string;
  productId: string;
  layerId: string;
  track: "live" | "day" | "archive";
  transport: "progressive-mp4" | "hls-ts";
  cadenceMinutes: number;
  width: number;
  height: number;
  viewport?: Record<string, number>;
  mediaViewport?: Record<string, number>;
  media: VideoMedia;
  defaultComposite?: VideoDefaultComposite;
  composites?: VideoCompositePreset[];
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

function validMedia(value: unknown, transport?: unknown): value is VideoMedia {
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
  const segmented = transport === "hls-ts"
    || media.mimeType === "application/vnd.apple.mpegurl";
  if (!segmented) return true;
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

function validCompositePreset(
  value: unknown,
  manifest: Partial<VideoLoopManifest>,
): value is VideoCompositePreset {
  if (!value || typeof value !== "object") return false;
  const composite = value as Partial<VideoCompositePreset>;
  const viewport = manifest.viewport ?? { left: 0, top: 0, width: 1, height: 1 };
  if (
    typeof composite.id !== "string"
    || !composite.id
    || !Array.isArray(composite.layerIds)
    || !composite.layerIds.length
    || composite.layerIds.some((id) => typeof id !== "string" || !id)
    || new Set(composite.layerIds).size !== composite.layerIds.length
    || !validViewport(composite.mediaViewport)
    || composite.mediaViewport.left !== viewport.left
    || composite.mediaViewport.top !== viewport.top
    || composite.mediaViewport.width !== viewport.width
    || composite.mediaViewport.height !== viewport.height
    || !Array.isArray(composite.ranges)
    || !composite.ranges.length
  ) return false;
  return composite.ranges.every((range) => {
    if (!range || typeof range !== "object") return false;
    const candidate = range as Partial<VideoCompositeRange>;
    if (
      !finitePositive(candidate.hours)
      || !Number.isInteger(candidate.firstFrame)
      || Number(candidate.firstFrame) < 0
      || !Number.isInteger(candidate.frameCount)
      || Number(candidate.frameCount) < 2
      || Number(candidate.firstFrame) + Number(candidate.frameCount) > (manifest.frames?.length ?? 0)
      || !Array.isArray(candidate.durationsSeconds)
      || candidate.durationsSeconds.length !== candidate.frameCount
      || !candidate.durationsSeconds.every(finitePositive)
      || candidate.boundaryIntervalMultiplier !== 4
      || !Array.isArray(candidate.renditions)
      || !candidate.renditions.length
    ) return false;
    return candidate.renditions.every((rendition) => (
      Boolean(rendition)
      && typeof rendition.id === "string"
      && Boolean(rendition.id)
      && validMedia(rendition.media)
      && (rendition.media.contentHeight ?? rendition.media.height) > 0
    ));
  });
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
    ![1, 2].includes(Number(manifest.schemaVersion))
    || typeof manifest.generation !== "string"
    || typeof manifest.generatedAt !== "string"
    || typeof manifest.productId !== "string"
    || typeof manifest.layerId !== "string"
    || !["live", "day", "archive"].includes(String(manifest.track))
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
  const composites = parsed.composites?.filter((value) => (
    validCompositePreset(value, parsed)
  ));
  return { ...parsed, composites: composites?.length ? composites : undefined };
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

export function exactCompositeManifest(
  manifest: VideoLoopManifest,
  layerIds: readonly string[],
  rangeHours: number,
  preferredRendition: "efficient" | "high" | "auto" = "auto",
): { manifest: VideoLoopManifest; presetId: string; renditionId: string } | null {
  const preset = manifest.composites?.find((candidate) => {
    const left = new Set(candidate.layerIds);
    return left.size === layerIds.length && layerIds.every((id) => left.has(id));
  });
  const range = preset?.ranges.find((candidate) => candidate.hours === rangeHours);
  if (!preset || !range) return null;
  const rendition = preferredRendition === "efficient"
    ? range.renditions.find((candidate) => candidate.id === "efficient")
      ?? range.renditions[0]
    : preferredRendition === "high"
      ? range.renditions.find((candidate) => candidate.id === "high")
        ?? range.renditions[range.renditions.length - 1]
      : range.renditions[range.renditions.length - 1];
  if (!rendition) return null;
  let pts = 0;
  const frames = manifest.frames
    .slice(range.firstFrame, range.firstFrame + range.frameCount)
    .map((frame, index) => {
      const durationSeconds = range.durationsSeconds[index];
      const result = {
        ...frame,
        index,
        ptsSeconds: pts,
        durationSeconds,
      };
      pts += durationSeconds;
      return result;
    });
  return {
    presetId: preset.id,
    renditionId: rendition.id,
    manifest: {
      ...manifest,
      transport: rendition.media.mimeType === "application/vnd.apple.mpegurl"
        ? "hls-ts"
        : "progressive-mp4",
      width: rendition.media.width,
      height: rendition.media.contentHeight ?? rendition.media.height,
      mediaViewport: preset.mediaViewport,
      media: rendition.media,
      frames,
    },
  };
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
