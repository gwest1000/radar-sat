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

// Validate decoded media pixels, not HTMLVideoElement's intrinsic display size.
// Native HLS can report provisional display dimensions at loadedmetadata;
// display dimensions also account for aperture/aspect ratio and need not equal
// the encoded pixels. A delivered frame is the reliable point for this check.
export function decodedVideoDimensionsError(
  frame: { width: number; height: number },
  expected: { width: number; height: number },
): string | null {
  if (frame.width === expected.width && frame.height === expected.height) return null;
  return `Decoded video dimensions ${frame.width}×${frame.height} do not match its manifest (${expected.width}×${expected.height}).`;
}
