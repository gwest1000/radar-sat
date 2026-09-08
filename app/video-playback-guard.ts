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

// Native HLS handles buffering and decoding on Safari/iPad. MediaSource support
// alone is not a reason to route that browser through the JavaScript player.
export function selectHlsEngine(nativeSupported: boolean, javascriptSupported: boolean): "native" | "hls-js" | "unavailable" {
  if (nativeSupported) return "native";
  return javascriptSupported ? "hls-js" : "unavailable";
}
