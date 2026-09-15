# Prebuilt views — September 2026

The menu is an inventory of verified, published loop manifests. It does not
advertise a recipe until its media exists. Exact MP4 sidecars serve 3–24 hours;
archive bundles expose the same recipe names for 7 days. Hybrid core presets
are retired, including the old scheduler lane and stale environment override.

| Region | Durations | Recipes | Satellite |
| --- | --- | --- | --- |
| BC XL | 3, 6, 12, 24 h; 7 d | Full; Full + Fire | Enhanced MSC GeoColour |
| BC | 3, 6, 12, 24 h | Full; Full + Fire | Enhanced MSC GeoColour |
| BC NE / SE / SW | 3, 6, 12, 24 h | Full; Full + Fire | Enhanced MSC GeoColour |
| South Coast | 3, 6, 12 h | Radar/Lightning; Radar/Lightning + Fire | Enhanced MSC GeoColour |
| E Pac/W NA | 12, 24 h; 7 d | Full; Full + Fire | NOAA VIS/IR |
| Pacific / North America | 12, 24 h; 7 d | Full | NOAA VIS/IR |

There are 60 combinations. Fire adds enhanced smoke and agency fire/thermal
hotspot icons. Full includes radar and lightning; BC XL, BC and the broad
views also include MSLP/500 hPa. Regional model contours remain optional
custom layers. Transmission lines are absent from the three broad products.
South Coast transmission width is reduced by 15%, with a slightly wider
watershed core. The three BC quadrant crops retain their centers and aspect
ratios while their width/height are divided by 1.15; output dimensions stay
unchanged. Regional overlay metadata prevents old cropped rasters from being
reused at a new viewport.

## Enhancement and cost

All BC MSC prebuilt tracks use source-preserving layered daylight, soft colour,
50% depth and CRF 22. No model inference, generated clouds, pixel displacement,
or invented cloud geometry is involved. Each viewport has a projected geographic
lookup for solar direction, daylight gating, convergence and map distortion.
The accepted BC XL lookup is retained exactly. Longitudes are unwrapped before
interpolation over the Pacific dateline. Night pixels retain their source grade.

Exact and archive rendering share the same geographic grid and lossless grade
cache, reused across fire recipes, durations and renditions. A failed grade
fails that generation; a differently styled last frame is never spliced into
an enhanced circuit. A representative 1920-pixel BC grade took roughly 0.9–1.0
seconds on the production host; a 1200-pixel Pacific trial took about 0.5 seconds.
These are uncached frame timings, not full-loop build-time guarantees.

## NOAA and WestWX

WestWX is a local processing path for NOAA GOES-18 calibrated imagery, with its
own true-colour/neutral-infrared rendering. NOAA VIS/IR uses the NOAA STAR/CIRA
GeoColor image already downloaded for the Pacific. The same source download
now also projects directly to North America, skipping already-rendered targets.
Both are NOAA observations; differences come from the rendering and coverage.
GeoColor uses a daytime true-colour image and a different infrared composite at
night; WestWX uses its own neutral infrared blend across twilight.
The GOES-West eastern edge remains less well observed than the western domain.
NOAA IR retains its existing calibrated infrared source.

North America had no retained NOAA STAR archive at migration time. Existing
WestWX observations bridge that history with explicit transition provenance;
new and available recent slots become NOAA STAR GeoColor. Do not relabel old
observation times or interpolate synthetic intermediate observations. This
bounded migration avoids downloading seven days of large raw ABI files.

A source-preserving NOAA enhancement comparison was rendered separately. NOAA
prebuilts remain unenhanced pending appearance review; enabling it later requires
an explicit operational policy change and a complete, uniformly graded rebuild.
