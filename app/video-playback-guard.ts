export function shouldWaitForSequentialSurface({
  playing,
  fullyComposited,
  nativeLoop,
  currentIndex,
  frameCount,
  nextSurfaceReady,
}: {
  playing: boolean;
  fullyComposited: boolean;
  nativeLoop: boolean;
  currentIndex: number;
  frameCount: number;
  nextSurfaceReady: boolean;
}): boolean {
  if (!playing || fullyComposited || frameCount < 2 || nextSurfaceReady) return false;
  // Segmented playback already pauses on its final frame for the configured
  // boundary interval. Its boundary seek prepares frame zero before moving
  // the media clock, so it needs no separate sequential guard here.
  return nativeLoop || currentIndex < frameCount - 1;
}

export function selectHlsEngine(
  nativeSupported: boolean,
  javascriptSupported: boolean,
  userAgent: string,
): "native" | "hls-js" | "unavailable" {
  // Chromium now advertises native HLS too, but its native demuxer can stall
  // when our short VOD loops seek straight to the newest observation. Keep
  // hls.js on Chromium. Safari (including iPad's desktop user agent and iOS
  // browsers using WebKit) retains its native player.
  const appleWebKit = /AppleWebKit\//.test(userAgent)
    && !/(?:Chrome|Chromium|Edg|OPR)\//.test(userAgent)
    && !/Android/.test(userAgent);
  if (nativeSupported && (appleWebKit || !javascriptSupported)) return "native";
  return javascriptSupported ? "hls-js" : "unavailable";
}
