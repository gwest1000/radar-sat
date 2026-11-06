export type DisplayViewport = { left: number; top: number; width: number; height: number };

// Presentation-only crops of retained full-resolution media. No new source
// grids, proxy bitmaps, or video generations are needed for these subsets.
const CROPS: Record<string, DisplayViewport> = {
  // EPSG:3857 common grid: 61.5 N to 19.5 N, eastern tip of Maine (~66.9 W).
  "north-america-overlay": { left: 0, top: 0.338644, width: 0.824515, height: 0.527490 },
  // Pacific-centred Mercator: move the top from ~69 N to 61.5 N.
  "north-pacific-overlay": { left: 0, top: 0.254394, width: 0.77, height: 0.721542 },
};

export function containsViewport(outer: DisplayViewport, inner: DisplayViewport): boolean {
  const epsilon = 0.000001;
  return inner.width > 0 && inner.height > 0 && inner.left >= outer.left - epsilon
    && inner.top >= outer.top - epsilon
    && inner.left + inner.width <= outer.left + outer.width + epsilon
    && inner.top + inner.height <= outer.top + outer.height + epsilon;
}

export function displayViewport(productId: string, original: DisplayViewport, media?: DisplayViewport): DisplayViewport {
  const crop = CROPS[productId];
  return crop && containsViewport(original, crop) && (!media || containsViewport(media, crop)) ? crop : original;
}

export function viewportPlacement(source: DisplayViewport, target: DisplayViewport) {
  return {
    left: `${(source.left - target.left) / target.width * 100}%`,
    top: `${(source.top - target.top) / target.height * 100}%`,
    right: "auto", bottom: "auto",
    width: `${source.width / target.width * 100}%`,
    height: `${source.height / target.height * 100}%`,
  };
}
