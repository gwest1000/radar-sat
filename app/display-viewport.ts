export type DisplayViewport = { left: number; top: number; width: number; height: number };

// Presentation-only crops of retained full-resolution media. No new source
// grids, proxy bitmaps, or video generations are needed for these subsets.
const CROPS: Record<string, DisplayViewport> = {
  // BC Hydro Williston watershed's northernmost EPSG:3005 northing is
  // 1,449,268.399 m. Add 20 km above it; retain the BC width and aspect.
  "bc-small-overlay": { left: 0.164040, top: 0.317709392, width: 0.670919, height: 0.581179 },
  // EPSG:3857 common grid: 61.5 N to 19.5 N, eastern tip of Maine (~66.9 W).
  "north-america-overlay": { left: 0, top: 0.338644, width: 0.824515, height: 0.527490 },
  // Pacific-centred Mercator: move the top from ~69 N to 61.5 N.
  "north-pacific-overlay": { left: 0, top: 0.254394, width: 0.77, height: 0.721542 },
};

type ViewProduct = {
  id: string; title: string; shortTitle: string; domain: string;
  viewport?: DisplayViewport; defaultHours: number; maxHours?: number; notes: string[];
};

// Keep the selected view identity separate from the real media identity.
// BC XL already contains the shifted BC footprint, whereas old BC videos do
// not. Use the source product unchanged for recipe/profile/manifest validation;
// only the displayed name and requested crop belong to the selected BC view.
export function playbackProductForView<T extends ViewProduct>(selected: T | undefined, products: T[]): T | undefined {
  if (selected?.id !== "bc-small-overlay") return selected;
  const source = products.find((p) => p.id === "bc-large-overlay" && p.domain === selected.domain);
  if (!source || !containsViewport(source.viewport ?? { left: 0, top: 0, width: 1, height: 1 }, CROPS[selected.id])) return selected;
  // Prefer native BC media automatically once the backend eventually produces
  // the shifted footprint, without having to change the viewer again.
  if (containsViewport(selected.viewport ?? { left: 0, top: 0, width: 1, height: 1 }, CROPS[selected.id])) return selected;
  return { ...source, title: selected.title, shortTitle: selected.shortTitle,
    defaultHours: selected.defaultHours, maxHours: Math.min(selected.maxHours ?? 168, source.maxHours ?? 168),
    notes: [...source.notes, "The south-shifted BC view uses the existing BC XL media, cropped to the BC footprint."] };
}

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
