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
