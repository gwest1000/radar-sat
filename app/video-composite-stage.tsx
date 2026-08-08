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

const BITMAP_CACHE_BYTES = 192 * 1024 * 1024;
const FINAL_SURFACE_CACHE_SIZE = 4;
const VIDEO_PROGRESS_TIMEOUT_MS = 30_000;

class BitmapCache {
  private entries = new Map<string, BitmapEntry>();

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
      decodedBytes: layer.width * layer.height * 4,
      promise: Promise.resolve(undefined as unknown as ImageBitmap),
      references: 1,
      retired: false,
    };
    entry.promise = fetch(layer.url, { cache: "force-cache", mode: "cors" })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Video overlay returned ${response.status}.`);
        const bitmap = await createImageBitmap(await response.blob());
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
    for (const entry of this.entries.values()) this.retire(entry);
    this.entries.clear();
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

  constructor(
    private readonly bitmaps: BitmapCache,
    private readonly width: number,
    private readonly height: number,
  ) {}

  peek(key: string): PreparedSurfaces | undefined {
    return this.entries.get(key)?.value;
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
    while (this.entries.size > FINAL_SURFACE_CACHE_SIZE) {
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
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [bitmapCache] = useState(() => new BitmapCache());
  const [surfaceCache] = useState(() => new SurfaceCache(
    bitmapCache,
    manifest.width,
    manifest.height,
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

  const resizeCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const bounds = canvas.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    const sourceScale = Math.min(
      manifest.width / bounds.width,
      manifest.height / bounds.height,
    );
    const scale = Math.max(0.25, Math.min(window.devicePixelRatio || 1, sourceScale));
    const width = Math.max(1, Math.round(bounds.width * scale));
    const height = Math.max(1, Math.round(bounds.height * scale));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
  }, [manifest.height, manifest.width]);

  const draw = useCallback((surfaces: PreparedSurfaces) => {
    const canvas = canvasRef.current;
    const video = videoRef.current;
    if (!canvas || !video) throw new Error("Video display is unavailable.");
    resizeCanvas();
    const context = canvas.getContext("2d", { alpha: false });
    if (!context) throw new Error("Canvas rendering is unavailable.");
    context.save();
    context.clearRect(0, 0, canvas.width, canvas.height);
    if (surfaces.underlay) {
      context.globalAlpha = 1;
      context.drawImage(surfaces.underlay, 0, 0, canvas.width, canvas.height);
    }
    context.filter = satelliteFilter ?? "none";
    context.globalAlpha = 1;
    const displayViewport = manifest.viewport ?? { left: 0, top: 0, width: 1, height: 1 };
    const mediaViewport = manifest.mediaViewport ?? displayViewport;
    const sourceLeft = (
      (displayViewport.left - mediaViewport.left) / mediaViewport.width
    ) * video.videoWidth;
    const sourceTop = (
      (displayViewport.top - mediaViewport.top) / mediaViewport.height
    ) * video.videoHeight;
    const sourceWidth = (
      displayViewport.width / mediaViewport.width
    ) * video.videoWidth;
    const sourceHeight = (
      displayViewport.height / mediaViewport.height
    ) * video.videoHeight;
    context.drawImage(
      video,
      sourceLeft,
      sourceTop,
      sourceWidth,
      sourceHeight,
      0,
      0,
      canvas.width,
      canvas.height,
    );
    context.filter = "none";
    if (surfaces.overlay) {
      context.globalAlpha = 1;
      context.drawImage(surfaces.overlay, 0, 0, canvas.width, canvas.height);
    }
    context.restore();
  }, [manifest.mediaViewport, manifest.viewport, resizeCanvas, satelliteFilter]);

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
    const canvas = canvasRef.current;
    const surfaces = surfaceCache;
    if (!video || !canvas || !surfaces || !plans.length || failedRef.current) return;
    const index = videoFrameAtMediaTime(planFrames, mediaTime);
    const plan = plans[index];
    if (
      mediaTime + 0.001 < plan.frame.ptsSeconds
      || mediaTime >= plan.frame.ptsSeconds + plan.frame.durationSeconds + 0.001
    ) {
      seekToIndexRef.current(requestedIndexRef.current);
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
      draw(prepared);
      committedIndexRef.current = index;
      requestedIndexRef.current = index;
      presentedFramesRef.current += 1;
      lastProgressAtRef.current = Date.now();
      canvas.dataset.presentedFrames = String(presentedFramesRef.current);
      canvas.dataset.overlayStalls = String(overlayStallsRef.current);
      const quality = video.getVideoPlaybackQuality?.();
      canvas.dataset.videoDropped = String(quality?.droppedVideoFrames ?? 0);
      onFramePresented(index);
      for (let offset = 1; offset <= 2; offset += 1) {
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
    void surfaces.prepare(plan).then((prepared) => {
      if (commit(prepared) && playingRef.current && index < plans.length - 1) {
        playVideo(video);
      }
    }).catch((reason) => {
      if (operationEpochRef.current === operationEpoch) fail(reason);
    });
  }, [draw, fail, onFramePresented, planFrames, plans, playVideo, scheduleLoop, surfaceCache]);

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
    const canvas = canvasRef.current;
    const operationEpoch = invalidateOperation(video);
    surfaceCache.clear();
    if (loopTimerRef.current !== undefined) {
      window.clearTimeout(loopTimerRef.current);
      loopTimerRef.current = undefined;
    }
    if (
      !video
      || !canvas
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
      draw(prepared);
      committedIndexRef.current = index;
      requestedIndexRef.current = index;
      presentedFramesRef.current += 1;
      lastProgressAtRef.current = Date.now();
      canvas.dataset.presentedFrames = String(presentedFramesRef.current);
      canvas.dataset.overlayStalls = String(overlayStallsRef.current);
      const quality = video.getVideoPlaybackQuality?.();
      canvas.dataset.videoDropped = String(quality?.droppedVideoFrames ?? 0);
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
    draw,
    fail,
    invalidateOperation,
    onFramePresented,
    planRevision,
    plans,
    playVideo,
    requestedIndex,
    scheduleLoop,
    surfaceCache,
  ]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    if (manifest.transport === "hls-ts") {
      if (Hls.isSupported()) {
        const hls = new Hls({
          enableWorker: true,
          maxBufferLength: 120,
          maxMaxBufferLength: 300,
          backBufferLength: 30,
        });
        hls.on(Hls.Events.ERROR, (_event, data) => {
          const canvas = canvasRef.current;
          if (canvas) {
            canvas.dataset.hlsError = `${data.type}:${data.details}`;
          }
          if (data.fatal) fail(new Error(`Segmented H.264 playback failed: ${data.details}`));
        });
        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          const canvas = canvasRef.current;
          if (canvas) canvas.dataset.hlsState = "manifest-parsed";
        });
        hls.on(Hls.Events.BUFFER_APPENDED, () => {
          const canvas = canvasRef.current;
          if (canvas) {
            canvas.dataset.hlsState = "buffer-appended";
            canvas.dataset.hlsCurrentTime = String(video.currentTime);
            canvas.dataset.hlsReadyState = String(video.readyState);
            canvas.dataset.hlsDuration = String(video.duration);
            canvas.dataset.hlsBuffered = Array.from(
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
  }, [fail, manifest.media.mimeType, manifest.transport, mediaUrl]);

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
    if (committedIndexRef.current === requestedIndex) return;
    seekToIndexRef.current(requestedIndex);
  }, [requestedIndex]);

  useEffect(() => {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (!video || !canvas) return;
    const onMetadata = () => {
      canvas.dataset.hlsMetadata = `${video.videoWidth}x${video.videoHeight}@${video.duration}`;
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
    const observer = new ResizeObserver(() => {
      resizeCanvas();
      const current = plans[committedIndexRef.current];
      if (current) {
        const operationEpoch = operationEpochRef.current;
        const cacheKey = current.cacheKey;
        void surfaceCache.prepare(current).then((prepared) => {
          if (
            operationEpochRef.current === operationEpoch
            && plans[committedIndexRef.current]?.cacheKey === cacheKey
          ) draw(prepared);
        }).catch((reason) => {
          if (operationEpochRef.current === operationEpoch) fail(reason);
        });
      }
    });
    observer.observe(canvas);
    return () => {
      observer.disconnect();
      video.removeEventListener("loadedmetadata", onMetadata);
      video.removeEventListener("error", onError);
      video.removeEventListener("ended", onEnded);
    };
  }, [
    draw,
    fail,
    handleVideoFrame,
    manifest.media.height,
    manifest.media.mimeType,
    manifest.media.width,
    manifest.transport,
    plans,
    resizeCanvas,
    scheduleLoop,
    surfaceCache,
  ]);

  useLayoutEffect(() => {
    disposedRef.current = false;
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
    <>
      <canvas
        ref={canvasRef}
        className="video-composite-canvas"
        data-renderer="video"
        data-video-generation={manifest.generation}
        data-overlay-stalls="0"
        data-presented-frames="0"
        data-video-dropped="0"
        style={style}
      />
      <video
        ref={videoRef}
        className="video-loop-decoder"
        crossOrigin="anonymous"
        muted
        playsInline
        preload="auto"
        aria-hidden="true"
      />
    </>
  );
}
