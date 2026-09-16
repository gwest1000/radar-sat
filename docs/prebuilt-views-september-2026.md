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
| South Coast | 3, 6, 12 h | Radar/Lightning; Radar/Lightning + Fire | None (basemap) |
| E Pac/W NA | 12, 24 h; 7 d | Full; Full + Fire | Enhanced NOAA VIS/IR |
| Pacific / North America | 12, 24 h; 7 d | Full | Enhanced NOAA VIS/IR |

There are 60 combinations. Fire adds enhanced smoke and agency fire/thermal
hotspot icons. Full includes radar and lightning; BC XL, BC and the broad
views also include MSLP/500 hPa. Regional model contours remain optional
custom layers. Transmission lines are absent from the three broad products.
South Coast transmission width is reduced by 15%, with a slightly wider
watershed core. The three BC quadrant crops retain their centers and aspect
ratios while their width/height are divided successively by 1.15 and 1.10; output dimensions stay
unchanged. Regional overlay metadata prevents old cropped rasters from being
reused at a new viewport.

## Enhancement and cost

All BC MSC and broad NOAA VIS/IR prebuilt tracks use source-preserving layered daylight, soft colour,
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

The approved NOAA enhancement is enabled for all E Pac/W NA, Pacific and North
America prebuilt durations, including transitional WestWX observations. It uses
the same 50% soft-colour grade, per-domain solar geometry and CRF 22 encoding.
Complete enhanced generations replace untreated loops atomically. Original
observation rasters remain available for custom layers and regeneration; graded
PNGs stay in the existing shared 6 GB local cache and are not uploaded to R2.


## BC view refinements (15 September)

The South Coast exact recipes omit satellite entirely. Their existing MSC observation
clock still schedules the frames, but rendering begins with the dark basemap and no
satellite timestamp is advertised in the visible layer list. Satellite remains an
optional custom layer. Fire adds smoke and fire icons.

BC NE/SE/SW receive an additional centered 10% zoom, with unchanged display size and
aspect ratio. City labels retain their previous full-grid display scale. Separate
regional transmission rasters use a thinner line scale. BC
and BC XL coastlines, country borders and province/state borders use the same
thicker solid black stroke. The separate regional boundary style remains unchanged.

All encoders add eight repeated bottom-edge image rows before the remaining hidden
alternating clock strip. This prevents scaling/codec filtering from exposing a
flashing white line. The displayed dimensions and extent are unchanged. Cache and
video render versions invalidate older untreated encodes.

The Prebuilt Views menu opens on mouseover and remains available by click/touch.
Its popover starts below the trigger, so hover cannot put an option under a click
intended for the trigger. Headless desktop verification confirms this geometry.

Custom MSC GeoColour uses a native-resolution server-rendered enhanced derivative,
encoded once at WebP quality 88. Source rasters stay local encoder inputs; the public
MSC layer and rapid edge expose enhanced derivatives only. Each observation is
processed once, with source-correction/style invalidation and bounded advisory locks.
Publication projection is idempotent, and an unavailable enhancement is not replaced
by untreated imagery. Original NOAA imagery is no longer spliced into the custom MSC
layer. The initial 189-image retained archive took about 320 seconds with two workers.

### September 16: E Pac/NA consolidation and archive synchronization

The E Pac/W NA crop is retired from products, public catalog and both production
schedulers. North America is displayed as **E Pac/NA**, retaining its existing grid
and enhanced NOAA GeoColor source. It now has Full and Full + Fires at 12h, 24h and
7d. The total is 57 prebuilt combinations. Across the menu, the fire recipe is
**Full + Fires** (or **Radar/Lightning + Fires** for South Coast); subtracting smoke
and fire/hotspot layers gives Full. BC retains enhanced MSC GeoColour.

NE/SE/SW BC and South Coast now cap the duration menu at 24h, removing 7d without
adding any new prebuilt durations. BC and BC XL retain their existing choices.

E Pac/NA's seven-day encoder uses real hourly satellite slots, with two-minute
scan-start tolerance and no held intermediate images. Sparse legacy three-hour
history therefore advances all available layers on the same displayed snapshot.
Lightning scan-start tolerance matches satellite tolerance. Local retention,
remote expiry and bootstrap downloads now preserve hourly North America
observations for seven days; ECMWF contours retain their three-hour source cycle.
Already-deleted intermediate rasters are not reconstructed. The existing sparse
portion ages out as hourly history accumulates. Other domains keep their current
three-hour observation archive retention, now correctly described by the catalog.
