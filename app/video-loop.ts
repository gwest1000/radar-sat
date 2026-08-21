export type VideoProfilePointer = {
  generation: string;
  manifestPath: string;
};

export type CompositeProfilePointer = VideoProfilePointer & {
  compositeKind?: "exact" | "hybrid-prefix";
  presetId: string;
  layerIds: string[];
  bakedLayerIds?: string[];
  eligibleOverlayLayerIds?: string[];
  rangeHours: number;
  generatedAt: string;
  endValidTime: string;
  endSourceTime: string;
};

export type VideoProxy = {
  path: string;
  width: number;
  height: number;
  byteLength: number;
  sha256?: string;
};

export type VideoProxyLayerSelection = {
  id: string;
  ids?: string[];
  renderId: string;
  sourceKey: string;
  sourceValidTime: string | null;
  sourceValidTimes?: Record<string, string | null>;
};

export type VideoManifestFrame = {
  index: number;
  validTime: string;
  sourceValidTime: string;
  sourceTimes?: Record<string, string>;
  layerSourceTimes?: Record<string, string | null>;
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

export type CompositeLoopFrame = {
  validTime: string;
  sourceValidTime: string;
  sourceTimes?: Record<string, string>;
  layerSourceTimes?: Record<string, string | null>;
  proxyLayers?: VideoProxyLayerSelection[];
  durationSeconds: number;
};

export type CompositeLoopManifest = {
  schemaVersion: 1 | 2;
  compositeKind?: "exact" | "hybrid-prefix";
  generation: string;
  generatedAt: string;
  productId: string;
  domainId: string;
  layerId: string;
  track: "live" | "day" | "archive";
  presetId: string;
  layerIds: string[];
  bakedLayerIds?: string[];
  eligibleOverlayLayerIds?: string[];
  rangeHours: number;
  cadenceMinutes: number;
  viewport: Record<string, number>;
  mediaViewport: Record<string, number>;
  endValidTime: string;
  endSourceTime: string;
  boundaryIntervalMultiplier: 4;
  frames: CompositeLoopFrame[];
  renditions: VideoCompositeRendition[];
  proxies?: Record<string, VideoProxy>;
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
const compositeManifestCache = new Map<string, Promise<CompositeLoopManifest>>();

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
    && proxy.byteLength >= 0
    && (
      proxy.sha256 === undefined
      || (typeof proxy.sha256 === "string" && proxy.sha256.length > 0)
    );
}

function validProxyLayerSelection(value: unknown): value is VideoProxyLayerSelection {
  if (!value || typeof value !== "object") return false;
  const layer = value as Partial<VideoProxyLayerSelection>;
  return typeof layer.id === "string"
    && layer.id.length > 0
    && typeof layer.renderId === "string"
    && layer.renderId.length > 0
    && typeof layer.sourceKey === "string"
    && layer.sourceKey.length > 0
    && (layer.ids === undefined || validLayerIds(layer.ids))
    && validSourceTimes(layer.sourceValidTimes, true)
    && (
      layer.sourceValidTime === null
      || (
        typeof layer.sourceValidTime === "string"
        && Number.isFinite(Date.parse(layer.sourceValidTime))
      )
    );
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
    && validSourceTimes(frame.layerSourceTimes, true)
    && Array.isArray(frame.proxyLayers)
    && frame.proxyLayers.every(validProxyLayerSelection);
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

function validLayerIds(value: unknown): value is string[] {
  return Array.isArray(value)
    && value.length > 0
    && value.every((id) => typeof id === "string" && id.length > 0)
    && new Set(value).size === value.length;
}

function validTimestamp(value: unknown): value is string {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

function validSourceTimes(value: unknown, nullable: boolean): value is Record<string, string | null> {
  if (value === undefined) return true;
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  return Object.entries(value).every(([key, timestamp]) => (
    Boolean(key)
    && ((nullable && timestamp === null) || validTimestamp(timestamp))
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
  const defaultComposite = parsed.defaultComposite !== undefined
    && validDefaultComposite(parsed.defaultComposite, parsed)
    ? parsed.defaultComposite
    : undefined;
  const composites = Array.isArray(parsed.composites)
    ? parsed.composites.filter((value) => validCompositePreset(value, parsed))
    : undefined;
  return {
    ...parsed,
    defaultComposite,
    composites: composites?.length ? composites : undefined,
  };
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

export function parseCompositeProfilePointer(value: unknown): CompositeProfilePointer {
  if (!value || typeof value !== "object") {
    throw new Error("Composite profile pointer is not an object.");
  }
  const pointer = value as Partial<CompositeProfilePointer>;
  const hybrid = pointer.compositeKind === "hybrid-prefix";
  const pointerLayerIds = pointer.layerIds ?? [];
  if (
    typeof pointer.generation !== "string"
    || !pointer.generation
    || typeof pointer.manifestPath !== "string"
    || !pointer.manifestPath
    || typeof pointer.presetId !== "string"
    || !pointer.presetId
    || !validLayerIds(pointer.layerIds)
    || !finitePositive(pointer.rangeHours)
    || !validTimestamp(pointer.generatedAt)
    || !validTimestamp(pointer.endValidTime)
    || !validTimestamp(pointer.endSourceTime)
    || (
      pointer.compositeKind !== undefined
      && !["exact", "hybrid-prefix"].includes(pointer.compositeKind)
    )
    || (
      hybrid
      && (
        !validLayerIds(pointer.bakedLayerIds)
        || !validLayerIds(pointer.eligibleOverlayLayerIds)
        || pointer.bakedLayerIds.length !== pointerLayerIds.length
        || pointer.bakedLayerIds.some((id, index) => id !== pointerLayerIds[index])
        || pointer.eligibleOverlayLayerIds.some((id) => pointerLayerIds.includes(id))
      )
    )
  ) {
    throw new Error("Composite profile pointer has an unsupported schema.");
  }
  return pointer as CompositeProfilePointer;
}

export function matchingCompositeProfile(
  values: readonly unknown[],
  layerIds: readonly string[],
  rangeHours: number,
): CompositeProfilePointer | null {
  const selected = new Set(layerIds);
  for (const value of values) {
    let pointer: CompositeProfilePointer;
    try {
      pointer = parseCompositeProfilePointer(value);
    } catch {
      continue;
    }
    if (
      pointer.compositeKind !== "hybrid-prefix"
      &&
      pointer.rangeHours === rangeHours
      && pointer.layerIds.length === selected.size
      && pointer.layerIds.every((id) => selected.has(id))
    ) return pointer;
  }
  return null;
}

export function matchingHybridCompositeProfile(
  values: readonly unknown[],
  orderedLayerIds: readonly string[],
  rangeHours: number,
): CompositeProfilePointer | null {
  const compatible: CompositeProfilePointer[] = [];
  for (const value of values) {
    let pointer: CompositeProfilePointer;
    try {
      pointer = parseCompositeProfilePointer(value);
    } catch {
      continue;
    }
    if (
      pointer.compositeKind !== "hybrid-prefix"
      || pointer.rangeHours !== rangeHours
    ) continue;
    const baked = pointer.bakedLayerIds ?? pointer.layerIds;
    const eligible = new Set(pointer.eligibleOverlayLayerIds ?? []);
    if (
      baked.length > orderedLayerIds.length
      || baked.some((id, index) => orderedLayerIds[index] !== id)
      || orderedLayerIds.slice(baked.length).some((id) => !eligible.has(id))
    ) continue;
    compatible.push(pointer);
  }
  compatible.sort((left, right) => {
    const bakedDifference = (right.bakedLayerIds?.length ?? right.layerIds.length)
      - (left.bakedLayerIds?.length ?? left.layerIds.length);
    if (bakedDifference) return bakedDifference;
    return Date.parse(right.generatedAt) - Date.parse(left.generatedAt);
  });
  return compatible[0] ?? null;
}

export function parseCompositeLoopManifest(value: unknown): CompositeLoopManifest {
  if (!value || typeof value !== "object") {
    throw new Error("Composite loop manifest is not an object.");
  }
  const manifest = value as Partial<CompositeLoopManifest>;
  const hybrid = manifest.compositeKind === "hybrid-prefix";
  if (
    ![1, 2].includes(Number(manifest.schemaVersion))
    || (hybrid && manifest.schemaVersion !== 2)
    || (
      manifest.compositeKind !== undefined
      && !["exact", "hybrid-prefix"].includes(manifest.compositeKind)
    )
    || typeof manifest.generation !== "string"
    || !manifest.generation
    || !validTimestamp(manifest.generatedAt)
    || typeof manifest.productId !== "string"
    || !manifest.productId
    || typeof manifest.domainId !== "string"
    || !manifest.domainId
    || typeof manifest.layerId !== "string"
    || !manifest.layerId
    || !["live", "day", "archive"].includes(String(manifest.track))
    || typeof manifest.presetId !== "string"
    || !manifest.presetId
    || !validLayerIds(manifest.layerIds)
    || !finitePositive(manifest.rangeHours)
    || !finitePositive(manifest.cadenceMinutes)
    || !validViewport(manifest.viewport)
    || !validViewport(manifest.mediaViewport)
    || !validTimestamp(manifest.endValidTime)
    || !validTimestamp(manifest.endSourceTime)
    || manifest.boundaryIntervalMultiplier !== 4
    || !Array.isArray(manifest.frames)
    || manifest.frames.length < 2
    || !Array.isArray(manifest.renditions)
    || !manifest.renditions.length
    || (
      hybrid
      && (
        !validLayerIds(manifest.bakedLayerIds)
        || !validLayerIds(manifest.eligibleOverlayLayerIds)
        || manifest.bakedLayerIds.length !== manifest.layerIds.length
        || manifest.bakedLayerIds.some((id, index) => id !== manifest.layerIds?.[index])
        || manifest.eligibleOverlayLayerIds.some((id) => manifest.layerIds?.includes(id))
        || !manifest.proxies
        || typeof manifest.proxies !== "object"
        || !Object.values(manifest.proxies).every((proxy) => (
          validProxy(proxy) && typeof proxy.sha256 === "string" && proxy.sha256.length > 0
        ))
      )
    )
  ) {
    throw new Error("Composite loop manifest has an unsupported schema.");
  }
  if (
    manifest.mediaViewport.left !== manifest.viewport.left
    || manifest.mediaViewport.top !== manifest.viewport.top
    || manifest.mediaViewport.width !== manifest.viewport.width
    || manifest.mediaViewport.height !== manifest.viewport.height
  ) {
    throw new Error("Composite loop manifest viewport does not match its product.");
  }
  let previousValidTime = -Infinity;
  let previousSourceTime = -Infinity;
  const eligibleOverlayLayerIds = new Set(manifest.eligibleOverlayLayerIds ?? []);
  for (const frame of manifest.frames) {
    if (
      !frame
      || typeof frame !== "object"
      || !validTimestamp(frame.validTime)
      || !validTimestamp(frame.sourceValidTime)
      || !finitePositive(frame.durationSeconds)
      || !validSourceTimes(frame.sourceTimes, false)
      || !validSourceTimes(frame.layerSourceTimes, true)
      || (
        hybrid
        && (
          !Array.isArray(frame.proxyLayers)
          || !frame.proxyLayers.every((selection) => (
            validProxyLayerSelection(selection)
            && (selection.ids ?? [selection.id]).every((id) => (
              eligibleOverlayLayerIds.has(id)
            ))
            && Boolean(manifest.proxies?.[selection.sourceKey])
          ))
          || new Set(frame.proxyLayers.map((selection) => selection.id)).size
            !== frame.proxyLayers.length
        )
      )
    ) {
      throw new Error("Composite loop manifest contains an invalid frame.");
    }
    const validTime = Date.parse(frame.validTime);
    const sourceTime = Date.parse(frame.sourceValidTime);
    if (validTime <= previousValidTime || sourceTime < previousSourceTime) {
      throw new Error("Composite loop manifest times are not monotonic.");
    }
    previousValidTime = validTime;
    previousSourceTime = sourceTime;
  }
  const finalFrame = manifest.frames[manifest.frames.length - 1];
  if (
    Date.parse(manifest.endValidTime) !== Date.parse(finalFrame.validTime)
    || Date.parse(manifest.endSourceTime) !== Date.parse(finalFrame.sourceValidTime)
  ) {
    throw new Error("Composite loop manifest endpoint does not match its final frame.");
  }
  if (!manifest.renditions.every((value) => (
    Boolean(value)
    && typeof value.id === "string"
    && Boolean(value.id)
    && validMedia(value.media, value.media?.mimeType === "application/vnd.apple.mpegurl" ? "hls-ts" : "progressive-mp4")
  ))) {
    throw new Error("Composite loop manifest contains an invalid rendition.");
  }
  return manifest as CompositeLoopManifest;
}

export function loadCompositeLoopManifest(url: string): Promise<CompositeLoopManifest> {
  const existing = compositeManifestCache.get(url);
  if (existing) {
    compositeManifestCache.delete(url);
    compositeManifestCache.set(url, existing);
    return existing;
  }
  const request = fetch(url, { cache: "force-cache", mode: "cors" })
    .then(async (response) => {
      if (!response.ok) throw new Error(`Composite manifest returned ${response.status}.`);
      return parseCompositeLoopManifest(await response.json());
    })
    .catch((error) => {
      if (compositeManifestCache.get(url) === request) compositeManifestCache.delete(url);
      throw error;
    });
  compositeManifestCache.set(url, request);
  while (compositeManifestCache.size > MAX_MANIFEST_CACHE_ENTRIES) {
    const oldest = compositeManifestCache.keys().next().value;
    if (typeof oldest !== "string") break;
    compositeManifestCache.delete(oldest);
  }
  return request;
}

export function compositeLoopVideoManifest(
  manifest: CompositeLoopManifest,
  preferredRendition: "efficient" | "high" | "auto" = "auto",
): { manifest: VideoLoopManifest; presetId: string; renditionId: string } | null {
  const rendition = preferredRendition === "efficient"
    ? manifest.renditions.find((candidate) => candidate.id === "efficient")
      ?? manifest.renditions[0]
    : preferredRendition === "high"
      ? manifest.renditions.find((candidate) => candidate.id === "high")
        ?? manifest.renditions[manifest.renditions.length - 1]
      : manifest.renditions[manifest.renditions.length - 1];
  if (!rendition) return null;
  let pts = 0;
  const frames: VideoManifestFrame[] = manifest.frames.map((frame, index) => {
    const result: VideoManifestFrame = {
      index,
      validTime: frame.validTime,
      sourceValidTime: frame.sourceValidTime,
      sourceTimes: frame.sourceTimes,
      layerSourceTimes: frame.layerSourceTimes,
      encodedSourceLayer: manifest.layerId,
      sourcePath: "",
      sourceFetchedAt: manifest.generatedAt,
      ptsSeconds: pts,
      durationSeconds: frame.durationSeconds,
      proxyLayers: frame.proxyLayers ?? Object.entries(frame.layerSourceTimes ?? {}).map(
        ([id, sourceValidTime]) => ({
          id,
          renderId: id,
          sourceKey: `composite:${id}:${sourceValidTime ?? "static"}`,
          sourceValidTime,
        }),
      ),
    };
    pts += frame.durationSeconds;
    return result;
  });
  return {
    presetId: manifest.presetId,
    renditionId: rendition.id,
    manifest: {
      schemaVersion: 2,
      generation: manifest.generation,
      generatedAt: manifest.generatedAt,
      productId: manifest.productId,
      layerId: manifest.layerId,
      track: manifest.track,
      transport: rendition.media.mimeType === "application/vnd.apple.mpegurl"
        ? "hls-ts"
        : "progressive-mp4",
      cadenceMinutes: manifest.cadenceMinutes,
      width: rendition.media.width,
      height: rendition.media.contentHeight ?? rendition.media.height,
      viewport: manifest.viewport,
      mediaViewport: manifest.mediaViewport,
      media: rendition.media,
      frames,
      proxies: manifest.proxies ?? {},
    },
  };
}

export function videoFrameSourceTimeMap(
  frame: Pick<
    VideoManifestFrame,
    "sourceValidTime" | "layerSourceTimes" | "proxyLayers"
  >,
): Map<string, number> {
  const sourceTimes = new Map<string, number>();
  const remember = (id: string, value: string | null | undefined) => {
    if (!id || !value) return;
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) sourceTimes.set(id, parsed);
  };
  for (const [id, sourceValidTime] of Object.entries(frame.layerSourceTimes ?? {})) {
    remember(id, sourceValidTime);
  }
  for (const selection of frame.proxyLayers) {
    remember(selection.id, selection.sourceValidTime);
    for (const id of selection.ids ?? [selection.id]) {
      remember(id, selection.sourceValidTimes?.[id] ?? selection.sourceValidTime);
    }
  }
  remember("satellite", frame.sourceValidTime);
  return sourceTimes;
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
