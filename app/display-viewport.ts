export type DisplayViewport = { left: number; top: number; width: number; height: number };

// Presentation crops in the normalized coordinates of the full source grid.
// A stored video can use a crop only if it covers that footprint.
const CROPS: Record<string, DisplayViewport> = {
  // BC Hydro Williston watershed's northernmost EPSG:3005 northing is
  // 1,449,268.399 m. Add 20 km above it; retain the BC width and aspect.
  "bc-small-overlay": { left: 0.164040, top: 0.317709392, width: 0.670919, height: 0.581179 },
  // Keep 19.5 N and the eastern tip of Maine. Extend north to 63.66 N and
  // trim the western edge by 3.7 degrees to match the Pacific aspect ratio.
  "north-america-overlay": { left: 0.025, top: 0.296446855, width: 0.799515, height: 0.569687145 },
  // Keep 61.5 N and extend the southern edge from 9.19 N to 8.23 N.
  "north-pacific-overlay": { left: 0, top: 0.254394, width: 0.77, height: 0.731542 },
};

// Previously encoded Pacific loops end at 9.19 N. Keep their existing
// display crop until the scheduled replacement for each range is ready.
const PREVIOUS_PACIFIC_CROP: DisplayViewport = { left: 0, top: 0.254394, width: 0.77, height: 0.721542 };

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
  const fits = (candidate: DisplayViewport) => containsViewport(original, candidate)
    && (!media || containsViewport(media, candidate));
  if (crop && fits(crop)) return crop;
  if (productId === "north-pacific-overlay" && fits(PREVIOUS_PACIFIC_CROP)) return PREVIOUS_PACIFIC_CROP;
  return original;
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
