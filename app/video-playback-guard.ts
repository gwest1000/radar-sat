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
): "native" | "hls-js" | "unavailable" {
  // Prefer explicit buffering of our short VOD loops on every capable browser,
  // including iPad/iOS through MediaSource or ManagedMediaSource. Native HLS
  // controls its own short-segment fetching and fast-forward behavior; that
  // path can stutter repeatedly even after a complete circuit. The hls.js
  // support check is capability based, so desktop-mode user agents cannot
  // accidentally select a different player. Older devices retain native HLS.
  if (javascriptSupported) return "hls-js";
  return nativeSupported ? "native" : "unavailable";
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
