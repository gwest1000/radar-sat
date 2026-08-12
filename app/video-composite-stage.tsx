"use client";

import {
  CSSProperties,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Hls from "hls.js";

import {
  VideoLoopManifest,
  VideoManifestFrame,
  videoFrameAtMediaTime,
} from "./video-loop";

export type VideoCanvasProxyLayer = {
  url: string;
  opacity: number;
  width: number;
  height: number;
};

export type VideoCompositeFramePlan = {
  frame: VideoManifestFrame;
  cacheKey: string;
  underlays: VideoCanvasProxyLayer[];
  overlays: VideoCanvasProxyLayer[];
};

type PreparedSurfaces = {
  underlay?: HTMLCanvasElement;
  overlay?: HTMLCanvasElement;
};

type BitmapEntry = {
  promise: Promise<ImageBitmap>;
  bitmap?: ImageBitmap;
  decodedBytes: number;
  references: number;
  retired: boolean;
};

type BitmapLease = {
  bitmap: ImageBitmap;
  release: () => void;
};

type DecodeTask = {
  start: () => void;
  cancel: () => void;
};

const BITMAP_CACHE_BYTES = 128 * 1024 * 1024;
const MIN_SURFACE_CACHE_ENTRIES = 16;
const LOW_MEMORY_SURFACE_CACHE_BYTES = 256 * 1024 * 1024;
const STANDARD_SURFACE_CACHE_BYTES = 384 * 1024 * 1024;
const HIGH_MEMORY_SURFACE_CACHE_BYTES = 768 * 1024 * 1024;
const PLAYBACK_SURFACE_PIXELS = 1_300_000;
const VIDEO_PROGRESS_TIMEOUT_MS = 30_000;
const HLS_BUFFER_BYTES = 48 * 1024 * 1024;
const HLS_LIVE_BUFFER_SECONDS = 40;
const HLS_ARCHIVE_BUFFER_SECONDS = 45;
const HLS_ARCHIVE_MAX_BUFFER_SECONDS = 60;
const HLS_BACK_BUFFER_SECONDS = 15;
const MAX_CONCURRENT_OVERLAY_DECODES = 3;

function playbackSurfaceSize(width: number, height: number): { width: number; height: number } {
  const scale = Math.min(1, Math.sqrt(PLAYBACK_SURFACE_PIXELS / (width * height)));
  return {
    width: Math.max(2, Math.round(width * scale / 2) * 2),
    height: Math.max(2, Math.round(height * scale / 2) * 2),
  };
}

function playbackLookahead(speed: number): number {
  if (speed >= 4) return 12;
  if (speed >= 3) return 8;
  if (speed >= 2) return 4;
  return 2;
}

function surfaceCacheBudgetBytes(): number {
  const deviceMemory = Number(
    (navigator as Navigator & { deviceMemory?: number }).deviceMemory ?? 4,
  );
  if (deviceMemory >= 8) return HIGH_MEMORY_SURFACE_CACHE_BYTES;
  if (deviceMemory >= 4) return STANDARD_SURFACE_CACHE_BYTES;
  return LOW_MEMORY_SURFACE_CACHE_BYTES;
}

function surfaceCacheEntryLimit(
  plans: VideoCompositeFramePlan[],
  width: number,
  height: number,
  budgetBytes: number,
): number {
  const hasUnderlay = plans.some((plan) => plan.underlays.length > 0);
  const hasOverlay = plans.some((plan) => plan.overlays.length > 0);
  const surfacesPerFrame = Math.max(1, Number(hasUnderlay) + Number(hasOverlay));
  const bytesPerFrame = width * height * 4 * surfacesPerFrame;
  const budgetEntries = Math.max(1, Math.floor(budgetBytes / bytesPerFrame));
  return Math.min(
    plans.length,
    Math.max(MIN_SURFACE_CACHE_ENTRIES, budgetEntries),
  );
}

class BitmapCache {
  private entries = new Map<string, BitmapEntry>();
  private activeDecodes = 0;
  private decodeQueue: DecodeTask[] = [];
  private disposed = false;

  constructor(
    private readonly width: number,
    private readonly height: number,
  ) {}

  acquire(layer: VideoCanvasProxyLayer): Promise<BitmapLease> {
    const existing = this.entries.get(layer.url);
    if (existing) {
      this.entries.delete(layer.url);
      this.entries.set(layer.url, existing);
      existing.references += 1;
      return existing.promise.then((bitmap) => ({
        bitmap,
        release: () => this.release(layer.url, existing),
      }));
    }
    const entry: BitmapEntry = {
      decodedBytes: this.width * this.height * 4,
      promise: Promise.resolve(undefined as unknown as ImageBitmap),
      references: 1,
      retired: false,
    };
    entry.promise = fetch(layer.url, { cache: "force-cache", mode: "cors" })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Video overlay returned ${response.status}.`);
        const bitmap = await this.decode(await response.blob());
        entry.bitmap = bitmap;
        return bitmap;
      })
      .catch((error) => {
        if (this.entries.get(layer.url) === entry) this.entries.delete(layer.url);
        throw error;
    });
    this.entries.set(layer.url, entry);
    return entry.promise.then((bitmap) => ({
      bitmap,
      release: () => this.release(layer.url, entry),
    }));
  }

  clear(): void {
    this.disposed = true;
    for (const task of this.decodeQueue) task.cancel();
    this.decodeQueue = [];
    for (const entry of this.entries.values()) this.retire(entry);
    this.entries.clear();
  }

  activate(): void {
    this.disposed = false;
  }

  private decode(blob: Blob): Promise<ImageBitmap> {
    return new Promise((resolve, reject) => {
      if (this.disposed) {
        reject(new DOMException("Overlay decoding was cancelled.", "AbortError"));
        return;
      }
      const start = () => {
        this.activeDecodes += 1;
        void createImageBitmap(blob, {
          resizeWidth: this.width,
          resizeHeight: this.height,
          resizeQuality: "high",
        }).then(resolve, reject).finally(() => {
          this.activeDecodes = Math.max(0, this.activeDecodes - 1);
          this.startQueuedDecodes();
        });
      };
      this.decodeQueue.push({
        start,
        cancel: () => reject(new DOMException("Overlay decoding was cancelled.", "AbortError")),
      });
      this.startQueuedDecodes();
    });
  }

  private startQueuedDecodes(): void {
    while (
      this.activeDecodes < MAX_CONCURRENT_OVERLAY_DECODES
      && this.decodeQueue.length
    ) {
      this.decodeQueue.shift()?.start();
    }
  }

  private trim(): void {
    let bytes = [...this.entries.values()].reduce((sum, entry) => sum + entry.decodedBytes, 0);
    while (bytes > BITMAP_CACHE_BYTES && this.entries.size > 1) {
      const oldestKey = [...this.entries.entries()]
        .find(([, entry]) => entry.references === 0 && Boolean(entry.bitmap))?.[0];
      if (!oldestKey) break;
      const oldest = this.entries.get(oldestKey);
      this.entries.delete(oldestKey);
      if (oldest) {
        bytes -= oldest.decodedBytes;
        this.retire(oldest);
      }
    }
  }

  private release(url: string, entry: BitmapEntry): void {
    entry.references = Math.max(0, entry.references - 1);
    if (entry.retired) this.closeIfUnused(entry);
    else if (this.entries.get(url) === entry) this.trim();
  }

  private retire(entry: BitmapEntry): void {
    entry.retired = true;
    this.closeIfUnused(entry);
  }

  private closeIfUnused(entry: BitmapEntry): void {
    if (!entry.bitmap || entry.references > 0) return;
    entry.bitmap.close();
    entry.bitmap = undefined;
  }
}

class SurfaceCache {
  private entries = new Map<string, {
    promise: Promise<PreparedSurfaces>;
    value?: PreparedSurfaces;
  }>();
  private preparedCount = 0;

  constructor(
    private readonly bitmaps: BitmapCache,
    private readonly width: number,
    private readonly height: number,
    private maxEntries: number,
  ) {}

  get size(): number {
    return this.entries.size;
  }

  get limit(): number {
    return this.maxEntries;
  }

  get builds(): number {
    return this.preparedCount;
  }

  setLimit(value: number): void {
    this.maxEntries = Math.max(1, value);
    this.trim();
  }

  peek(key: string): PreparedSurfaces | undefined {
    const entry = this.entries.get(key);
    if (!entry) return undefined;
    this.entries.delete(key);
    this.entries.set(key, entry);
    return entry.value;
  }

  prepare(plan: VideoCompositeFramePlan): Promise<PreparedSurfaces> {
    const existing = this.entries.get(plan.cacheKey);
    if (existing) {
      this.entries.delete(plan.cacheKey);
      this.entries.set(plan.cacheKey, existing);
      return existing.promise;
    }
    const entry: { promise: Promise<PreparedSurfaces>; value?: PreparedSurfaces } = {
      promise: Promise.resolve({}),
    };
    entry.promise = Promise.all([
      this.render(plan.underlays),
      this.render(plan.overlays),
    ]).then(([underlay, overlay]) => {
      entry.value = { underlay, overlay };
      return entry.value;
    });
    this.entries.set(plan.cacheKey, entry);
    this.preparedCount += 1;
    this.trim();
    return entry.promise;
  }

  clear(): void {
    for (const entry of this.entries.values()) {
      void entry.promise.then((surfaces) => {
        for (const surface of [surfaces.underlay, surfaces.overlay]) {
          if (surface) {
            surface.width = 0;
            surface.height = 0;
          }
        }
      }).catch(() => undefined);
    }
    this.entries.clear();
    this.preparedCount = 0;
  }

  private async render(layers: VideoCanvasProxyLayer[]): Promise<HTMLCanvasElement | undefined> {
    if (!layers.length) return undefined;
    const results = await Promise.allSettled(layers.map(async (layer) => ({
      layer,
      lease: await this.bitmaps.acquire(layer),
    })));
    const failed = results.find((result) => result.status === "rejected");
    if (failed?.status === "rejected") {
      for (const result of results) {
        if (result.status === "fulfilled") result.value.lease.release();
      }
      throw failed.reason;
    }
    const decoded = results.flatMap((result) => result.status === "fulfilled" ? [result.value] : []);
    try {
      const canvas = document.createElement("canvas");
      canvas.width = this.width;
      canvas.height = this.height;
      const context = canvas.getContext("2d", { alpha: true });
      if (!context) throw new Error("Canvas rendering is unavailable.");
      context.clearRect(0, 0, canvas.width, canvas.height);
      for (const { layer, lease } of decoded) {
        context.globalAlpha = layer.opacity;
        context.drawImage(lease.bitmap, 0, 0, canvas.width, canvas.height);
      }
      context.globalAlpha = 1;
      return canvas;
    } finally {
      for (const { lease } of decoded) lease.release();
    }
  }

  private trim(): void {
    while (this.entries.size > this.maxEntries) {
      const oldestKey = this.entries.keys().next().value;
      if (typeof oldestKey !== "string") break;
      const entry = this.entries.get(oldestKey);
      this.entries.delete(oldestKey);
      if (entry) {
        void entry.promise.then((surfaces) => {
          for (const surface of [surfaces.underlay, surfaces.overlay]) {
            if (surface) {
              surface.width = 0;
              surface.height = 0;
            }
          }
        }).catch(() => undefined);
      }
    }
  }
}

export function VideoCompositeStage({
  manifest,
  mediaUrl,
  plans,
  requestedIndex,
  playing,
  speed,
  satelliteFilter,
  onFramePresented,
  onFailure,
  onLoopBoundary,
  style,
}: {
  manifest: VideoLoopManifest;
  mediaUrl: string;
  plans: VideoCompositeFramePlan[];
  requestedIndex: number;
  playing: boolean;
  speed: number;
  satelliteFilter?: string;
  onFramePresented: (index: number) => void;
  onFailure: (message: string) => void;
  onLoopBoundary?: () => void;
  style?: CSSProperties;
}) {
  const stageRef = useRef<HTMLDivElement>(null);
  const underlayHostRef = useRef<HTMLDivElement>(null);
  const overlayHostRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [surfaceSize] = useState(() => playbackSurfaceSize(manifest.width, manifest.height));
  const [surfaceBudgetBytes] = useState(surfaceCacheBudgetBytes);
  const surfaceEntryLimit = useMemo(
    () => surfaceCacheEntryLimit(
      plans,
      surfaceSize.width,
      surfaceSize.height,
      surfaceBudgetBytes,
    ),
    [plans, surfaceBudgetBytes, surfaceSize.height, surfaceSize.width],
  );
  const [bitmapCache] = useState(() => new BitmapCache(
    surfaceSize.width,
    surfaceSize.height,
  ));
  const [surfaceCache] = useState(() => new SurfaceCache(
    bitmapCache,
    surfaceSize.width,
    surfaceSize.height,
    surfaceEntryLimit,
  ));
  const callbackIdRef = useRef<number | undefined>(undefined);
  const loopTimerRef = useRef<number | undefined>(undefined);
  const committedIndexRef = useRef(-1);
  const requestedIndexRef = useRef(requestedIndex);
  const playingRef = useRef(playing);
  const speedRef = useRef(speed);
  const failedRef = useRef(false);
  const overlayStallsRef = useRef(0);
  const presentedFramesRef = useRef(0);
  const lastProgressAtRef = useRef(0);
  const seekingRef = useRef(false);
  const disposedRef = useRef(false);
  const operationEpochRef = useRef(0);
  const seekedListenerRef = useRef<(() => void) | undefined>(undefined);
  const planFrames = useMemo(() => plans.map((plan) => plan.frame), [plans]);
  const planRevision = useMemo(
    () => `${satelliteFilter ?? "none"}\u0000${plans.map((plan) => plan.cacheKey).join("\u0000")}`,
    [plans, satelliteFilter],
  );
  const previousPlanRevisionRef = useRef(planRevision);

  useLayoutEffect(() => { playingRef.current = playing; }, [playing]);
  useEffect(() => { speedRef.current = speed; }, [speed]);
  useEffect(() => {
    surfaceCache.setLimit(surfaceEntryLimit);
  }, [surfaceCache, surfaceEntryLimit]);

  const invalidateOperation = useCallback((video = videoRef.current) => {
    operationEpochRef.current += 1;
    if (video && callbackIdRef.current !== undefined) {
      video.cancelVideoFrameCallback(callbackIdRef.current);
      callbackIdRef.current = undefined;
    }
    if (video && seekedListenerRef.current) {
      video.removeEventListener("seeked", seekedListenerRef.current);
      seekedListenerRef.current = undefined;
    }
    seekingRef.current = false;
    return operationEpochRef.current;
  }, []);

  const fail = useCallback((reason: unknown) => {
    // A layer/source change intentionally unmounts this decoder. Chromium can
    // deliver a final media error while the video element is being detached;
    // that teardown must not blacklist an otherwise healthy generation.
    if (disposedRef.current || failedRef.current) return;
    failedRef.current = true;
    invalidateOperation();
    const message = reason instanceof Error ? reason.message : String(reason);
    onFailure(message || "Video playback failed.");
  }, [invalidateOperation, onFailure]);

  const playVideo = useCallback((video: HTMLVideoElement) => {
    void video.play().catch((reason: unknown) => {
      // Pausing to wait for an atomic overlay or to seek a loop boundary
      // intentionally interrupts a pending play() promise in Chromium.
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      fail(reason);
    });
  }, [fail]);

  // Keep the full-resolution satellite in the browser's hardware video plane.
  // Canvas is reserved for transparent overlays, avoiding a full-frame
  // video-to-canvas copy at every meteorological timestep.
  const videoStyle = useMemo<CSSProperties>(() => {
    const displayViewport = manifest.viewport ?? { left: 0, top: 0, width: 1, height: 1 };
    const mediaViewport = manifest.mediaViewport ?? displayViewport;
    const contentHeight = manifest.media.contentHeight ?? manifest.media.height;
    const contentFraction = contentHeight / manifest.media.height;
    const left = (displayViewport.left - mediaViewport.left) / mediaViewport.width;
    const top = (
      (displayViewport.top - mediaViewport.top) / mediaViewport.height
    ) * contentFraction;
    const width = displayViewport.width / mediaViewport.width;
    const height = (displayViewport.height / mediaViewport.height) * contentFraction;
    return {
      width: `${100 / width}%`,
      height: `${100 / height}%`,
      left: `${-100 * left / width}%`,
      top: `${-100 * top / height}%`,
      filter: satelliteFilter ?? "none",
    };
  }, [
    manifest.media.contentHeight,
    manifest.media.height,
    manifest.mediaViewport,
    manifest.viewport,
    satelliteFilter,
  ]);

  const commitSurfaces = useCallback((surfaces: PreparedSurfaces) => {
    const underlayHost = underlayHostRef.current;
    const overlayHost = overlayHostRef.current;
    if (!underlayHost || !overlayHost) throw new Error("Video display is unavailable.");
    underlayHost.replaceChildren(...(surfaces.underlay ? [surfaces.underlay] : []));
    overlayHost.replaceChildren(...(surfaces.overlay ? [surfaces.overlay] : []));
  }, []);

  const requestFrameRef = useRef<() => void>(() => undefined);
  const seekToIndexRef = useRef<(index: number) => void>(() => undefined);

  const scheduleLoop = useCallback((index: number) => {
    const video = videoRef.current;
    if (
      !video
      || !playingRef.current
      || index !== plans.length - 1
      || loopTimerRef.current !== undefined
    ) return;
    video.pause();
    const delay = (plans[index].frame.durationSeconds * 1_000 + 215) / speedRef.current;
    loopTimerRef.current = window.setTimeout(() => {
      loopTimerRef.current = undefined;
      onLoopBoundary?.();
      seekToIndexRef.current(0);
    }, delay);
  }, [onLoopBoundary, plans]);

  const handleVideoFrame = useCallback((mediaTime: number) => {
    const video = videoRef.current;
    const stage = stageRef.current;
    const surfaces = surfaceCache;
    if (!video || !stage || !surfaces || !plans.length || failedRef.current) return;
    const index = videoFrameAtMediaTime(planFrames, mediaTime);
    const plan = plans[index];
    if (
      mediaTime + 0.001 < plan.frame.ptsSeconds
      || mediaTime >= plan.frame.ptsSeconds + plan.frame.durationSeconds + 0.001
    ) {
      seekToIndexRef.current(requestedIndexRef.current);
      return;
    }
    // Some decoders can emit a redundant callback at a segment transition.
    // It maps to the frame already on screen, so skip the duplicate canvas
    // composition and wait for the next meteorological frame callback.
    if (index === committedIndexRef.current && !seekingRef.current) {
      lastProgressAtRef.current = Date.now();
      requestFrameRef.current();
      return;
    }
    const ready = surfaces.peek(plan.cacheKey);
    const operationEpoch = operationEpochRef.current;
    const commit = (prepared: PreparedSurfaces): boolean => {
      if (
        failedRef.current
        || operationEpochRef.current !== operationEpoch
        || plans[index]?.cacheKey !== plan.cacheKey
      ) return false;
      commitSurfaces(prepared);
      committedIndexRef.current = index;
      requestedIndexRef.current = index;
      presentedFramesRef.current += 1;
      lastProgressAtRef.current = Date.now();
      stage.dataset.presentedFrames = String(presentedFramesRef.current);
      stage.dataset.overlayStalls = String(overlayStallsRef.current);
      const quality = video.getVideoPlaybackQuality?.();
      stage.dataset.videoDropped = String(quality?.droppedVideoFrames ?? 0);
      stage.dataset.surfaceCacheEntries = String(surfaces.size);
      stage.dataset.surfaceCacheLimit = String(surfaces.limit);
      stage.dataset.surfaceBuilds = String(surfaces.builds);
      stage.dataset.surfaceCacheMegabytes = String(Math.round(surfaceBudgetBytes / 1024 / 1024));
      onFramePresented(index);
      const lookahead = playbackLookahead(speedRef.current);
      for (let offset = 1; offset <= lookahead; offset += 1) {
        const candidate = plans[(index + offset) % plans.length];
        void surfaces.prepare(candidate).catch((reason) => {
          if (operationEpochRef.current === operationEpoch) fail(reason);
        });
      }
      if (index === plans.length - 1) scheduleLoop(index);
      else requestFrameRef.current();
      return true;
    };
    if (ready) {
      commit(ready);
      return;
    }
    overlayStallsRef.current += 1;
    video.pause();
    const lookahead = playbackLookahead(speedRef.current);
    const upcoming = Array.from(
      { length: Math.min(lookahead, Math.max(0, plans.length - 1)) },
      (_, offset) => plans[(index + offset + 1) % plans.length],
    );
    void Promise.all([
      surfaces.prepare(plan),
      ...upcoming.map((candidate) => surfaces.prepare(candidate)),
    ]).then(([prepared]) => {
      if (commit(prepared) && playingRef.current && index < plans.length - 1) {
        playVideo(video);
      }
    }).catch((reason) => {
      if (operationEpochRef.current === operationEpoch) fail(reason);
    });
  }, [commitSurfaces, fail, onFramePresented, planFrames, plans, playVideo, scheduleLoop, surfaceBudgetBytes, surfaceCache]);

  useEffect(() => {
    requestFrameRef.current = () => {
      const video = videoRef.current;
      if (!video || callbackIdRef.current !== undefined || failedRef.current) return;
      const operationEpoch = operationEpochRef.current;
      callbackIdRef.current = video.requestVideoFrameCallback((_now, metadata) => {
        callbackIdRef.current = undefined;
        if (operationEpochRef.current !== operationEpoch) return;
        handleVideoFrame(metadata.mediaTime);
      });
    };
  }, [handleVideoFrame]);

  useEffect(() => {
    seekToIndexRef.current = (index: number) => {
      const video = videoRef.current;
      if (!video || !plans.length || failedRef.current) return;
      if (video.readyState < HTMLMediaElement.HAVE_METADATA) return;
      const operationEpoch = invalidateOperation(video);
      if (loopTimerRef.current !== undefined) {
        window.clearTimeout(loopTimerRef.current);
        loopTimerRef.current = undefined;
      }
      const safeIndex = Math.max(0, Math.min(plans.length - 1, index));
      requestedIndexRef.current = safeIndex;
      seekingRef.current = true;
      video.pause();
      const target = plans[safeIndex].frame.ptsSeconds;
      const finishSeek = () => {
        if (operationEpochRef.current !== operationEpoch) return;
        seekedListenerRef.current = undefined;
        seekingRef.current = false;
        requestFrameRef.current();
        if (playingRef.current) playVideo(video);
      };
      if (Math.abs(video.currentTime - target) < 0.0005) {
        finishSeek();
        return;
      }
      const onSeeked = () => {
        finishSeek();
      };
      seekedListenerRef.current = onSeeked;
      video.addEventListener("seeked", onSeeked, { once: true });
      video.currentTime = target;
    };
  }, [invalidateOperation, plans, playVideo]);

  useLayoutEffect(() => {
    if (previousPlanRevisionRef.current === planRevision) return;
    previousPlanRevisionRef.current = planRevision;
    const video = videoRef.current;
    const stage = stageRef.current;
    const operationEpoch = invalidateOperation(video);
    surfaceCache.clear();
    if (loopTimerRef.current !== undefined) {
      window.clearTimeout(loopTimerRef.current);
      loopTimerRef.current = undefined;
    }
    if (
      !video
      || !stage
      || !plans.length
      || video.readyState < HTMLMediaElement.HAVE_METADATA
      || failedRef.current
    ) return;
    const index = Math.max(0, Math.min(plans.length - 1, requestedIndex));
    const plan = plans[index];
    requestedIndexRef.current = index;
    video.pause();
    if (Math.abs(video.currentTime - plan.frame.ptsSeconds) >= 0.0005) {
      seekToIndexRef.current(index);
      return;
    }
    void surfaceCache.prepare(plan).then((prepared) => {
      if (
        failedRef.current
        || operationEpochRef.current !== operationEpoch
        || plans[index]?.cacheKey !== plan.cacheKey
      ) return;
      commitSurfaces(prepared);
      committedIndexRef.current = index;
      requestedIndexRef.current = index;
      presentedFramesRef.current += 1;
      lastProgressAtRef.current = Date.now();
      stage.dataset.presentedFrames = String(presentedFramesRef.current);
      stage.dataset.overlayStalls = String(overlayStallsRef.current);
      const quality = video.getVideoPlaybackQuality?.();
      stage.dataset.videoDropped = String(quality?.droppedVideoFrames ?? 0);
      stage.dataset.surfaceCacheEntries = String(surfaceCache.size);
      stage.dataset.surfaceCacheLimit = String(surfaceCache.limit);
      stage.dataset.surfaceBuilds = String(surfaceCache.builds);
      stage.dataset.surfaceCacheMegabytes = String(Math.round(surfaceBudgetBytes / 1024 / 1024));
      onFramePresented(index);
      requestFrameRef.current();
      if (playingRef.current) {
        if (index === plans.length - 1) scheduleLoop(index);
        else playVideo(video);
      }
    }).catch((reason) => {
      if (operationEpochRef.current === operationEpoch) fail(reason);
    });
  }, [
    commitSurfaces,
    fail,
    invalidateOperation,
    onFramePresented,
    planRevision,
    plans,
    playVideo,
    requestedIndex,
    scheduleLoop,
    surfaceBudgetBytes,
    surfaceCache,
  ]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    if (manifest.transport === "hls-ts") {
      if (Hls.isSupported()) {
        // A live 24-hour weather loop is only about 25-33 seconds of encoded
        // media, so retaining it in full is what makes repeated 4x playback
        // smooth. Seven-day tracks are 2-4 minutes of media: cap those to a
        // one-minute forward window instead of allowing hls.js to retain most
        // or all of the archive in MediaSource. Already fetched immutable
        // segments remain available through the normal HTTP cache for seeks
        // and subsequent loops.
        const liveTrack = manifest.track === "live";
        const hls = new Hls({
          enableWorker: true,
          maxBufferSize: HLS_BUFFER_BYTES,
          maxBufferLength: liveTrack
            ? HLS_LIVE_BUFFER_SECONDS
            : HLS_ARCHIVE_BUFFER_SECONDS,
          maxMaxBufferLength: liveTrack
            ? HLS_LIVE_BUFFER_SECONDS
            : HLS_ARCHIVE_MAX_BUFFER_SECONDS,
          backBufferLength: HLS_BACK_BUFFER_SECONDS,
          // After an archive loops from its end back to zero, the former tail
          // becomes a disconnected future range. Without this threshold hls.js
          // leaves that range resident and repeated loops eventually refill the
          // entire archive despite the loading cap above.
          frontBufferFlushThreshold: liveTrack
            ? Number.POSITIVE_INFINITY
            : HLS_ARCHIVE_MAX_BUFFER_SECONDS,
        });
        hls.on(Hls.Events.ERROR, (_event, data) => {
          const stage = stageRef.current;
          if (stage) {
            stage.dataset.hlsError = `${data.type}:${data.details}`;
          }
          if (data.fatal) fail(new Error(`Segmented H.264 playback failed: ${data.details}`));
        });
        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          const stage = stageRef.current;
          if (stage) {
            stage.dataset.hlsState = "manifest-parsed";
            stage.dataset.hlsBufferTarget = String(
              liveTrack ? HLS_LIVE_BUFFER_SECONDS : HLS_ARCHIVE_BUFFER_SECONDS,
            );
            stage.dataset.hlsBufferMaximum = String(
              liveTrack ? HLS_LIVE_BUFFER_SECONDS : HLS_ARCHIVE_MAX_BUFFER_SECONDS,
            );
          }
        });
        hls.on(Hls.Events.BUFFER_APPENDED, () => {
          const stage = stageRef.current;
          if (stage) {
            stage.dataset.hlsState = "buffer-appended";
            stage.dataset.hlsCurrentTime = String(video.currentTime);
            stage.dataset.hlsReadyState = String(video.readyState);
            stage.dataset.hlsDuration = String(video.duration);
            stage.dataset.hlsBuffered = Array.from(
              { length: video.buffered.length },
              (_, index) => `${video.buffered.start(index)}-${video.buffered.end(index)}`,
            ).join(",");
          }
        });
        hls.loadSource(mediaUrl);
        hls.attachMedia(video);
        return () => hls.destroy();
      }
      if (video.canPlayType(manifest.media.mimeType)) {
        video.src = mediaUrl;
        return () => {
          video.removeAttribute("src");
          video.load();
        };
      }
      fail(new Error("Segmented H.264 playback is unavailable in this browser."));
      return;
    }
    video.src = mediaUrl;
    return () => {
      video.removeAttribute("src");
      video.load();
    };
  }, [fail, manifest.media.mimeType, manifest.track, manifest.transport, mediaUrl]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.playbackRate = speed;
    video.defaultPlaybackRate = speed;
    if (loopTimerRef.current !== undefined && committedIndexRef.current === plans.length - 1) {
      window.clearTimeout(loopTimerRef.current);
      loopTimerRef.current = undefined;
      scheduleLoop(committedIndexRef.current);
    }
  }, [plans.length, scheduleLoop, speed]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !video.readyState || seekingRef.current) return;
    if (!playing) {
      video.pause();
      if (loopTimerRef.current !== undefined) {
        window.clearTimeout(loopTimerRef.current);
        loopTimerRef.current = undefined;
      }
    } else if (committedIndexRef.current === plans.length - 1) {
      scheduleLoop(committedIndexRef.current);
    } else {
      playVideo(video);
    }
  }, [plans.length, playing, playVideo, scheduleLoop]);

  useEffect(() => {
    if (!playing || failedRef.current) return;
    lastProgressAtRef.current = Date.now();
    const interval = window.setInterval(() => {
      if (
        failedRef.current
        || !playingRef.current
        || seekingRef.current
        || loopTimerRef.current !== undefined
      ) {
        lastProgressAtRef.current = Date.now();
        return;
      }
      if (Date.now() - lastProgressAtRef.current > VIDEO_PROGRESS_TIMEOUT_MS) {
        fail(new Error("The H.264 loop stopped making progress; using image frames."));
      }
    }, 5_000);
    return () => window.clearInterval(interval);
  }, [fail, playing]);

  useLayoutEffect(() => {
    requestedIndexRef.current = requestedIndex;
    // During playback the video clock is authoritative. The parent deliberately
    // updates timeline/status state less often than the media track to avoid a
    // full React render for every decoded video frame.
    if (playingRef.current) return;
    if (committedIndexRef.current === requestedIndex) return;
    seekToIndexRef.current(requestedIndex);
  }, [requestedIndex]);

  useEffect(() => {
    const video = videoRef.current;
    const stage = stageRef.current;
    if (!video || !stage) return;
    const onMetadata = () => {
      stage.dataset.hlsMetadata = `${video.videoWidth}x${video.videoHeight}@${video.duration}`;
      if (
        video.videoWidth !== manifest.media.width
        || video.videoHeight !== manifest.media.height
      ) {
        fail(new Error("Video dimensions do not match its manifest."));
        return;
      }
      seekToIndexRef.current(requestedIndexRef.current);
    };
    const onError = () => {
      // hls.js emits a substantially more useful fatal-error detail than the
      // generic MediaSource error exposed by the video element. Let that
      // handler own failures when the browser is not using native HLS.
      if (
        manifest.transport === "hls-ts"
        && Hls.isSupported()
      ) return;
      const mediaError = video.error;
      const detail = mediaError
        ? ` (media error ${mediaError.code}${mediaError.message ? `: ${mediaError.message}` : ""})`
        : "";
      fail(new Error(
        `The H.264 loop could not be decoded${detail}`
        + ` (HLS engine ${Hls.isSupported() ? "available" : "unavailable"}, native HLS ${video.canPlayType(manifest.media.mimeType) || "unavailable"}).`,
      ));
    };
    const onEnded = () => {
      if (!playingRef.current || failedRef.current || !plans.length) return;
      const lastIndex = plans.length - 1;
      if (committedIndexRef.current === lastIndex) scheduleLoop(lastIndex);
      else handleVideoFrame(plans[lastIndex].frame.ptsSeconds);
    };
    video.addEventListener("loadedmetadata", onMetadata);
    video.addEventListener("error", onError);
    video.addEventListener("ended", onEnded);
    requestFrameRef.current();
    if (video.readyState >= HTMLMediaElement.HAVE_METADATA) onMetadata();
    return () => {
      video.removeEventListener("loadedmetadata", onMetadata);
      video.removeEventListener("error", onError);
      video.removeEventListener("ended", onEnded);
    };
  }, [
    fail,
    handleVideoFrame,
    manifest.media.height,
    manifest.media.mimeType,
    manifest.media.width,
    manifest.transport,
    plans,
    scheduleLoop,
  ]);

  useLayoutEffect(() => {
    disposedRef.current = false;
    bitmapCache.activate();
    const video = videoRef.current;
    return () => {
      disposedRef.current = true;
      invalidateOperation(video);
      if (loopTimerRef.current !== undefined) window.clearTimeout(loopTimerRef.current);
      surfaceCache.clear();
      bitmapCache.clear();
    };
  }, [bitmapCache, invalidateOperation, surfaceCache]);

  return (
    <div
      ref={stageRef}
      className="video-composite-canvas"
      data-renderer="video"
      data-video-generation={manifest.generation}
      data-overlay-stalls="0"
      data-presented-frames="0"
      data-video-dropped="0"
      data-surface-cache-entries="0"
      data-surface-cache-limit={surfaceEntryLimit}
      data-surface-builds="0"
      data-surface-cache-megabytes={Math.round(surfaceBudgetBytes / 1024 / 1024)}
      style={style}
    >
      <div ref={underlayHostRef} className="video-surface-layer video-underlay-layer" />
      <video
        ref={videoRef}
        className="video-loop-decoder"
        style={videoStyle}
        crossOrigin="anonymous"
        muted
        playsInline
        preload="auto"
        aria-hidden="true"
      />
      <div ref={overlayHostRef} className="video-surface-layer video-overlay-layer" />
    </div>
  );
}
