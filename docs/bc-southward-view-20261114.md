# Southward BC display — 14 November 2026

The BC view now targets normalized EPSG:3005 viewport
`{left:0.164040, top:0.317709392, width:0.670919, height:0.581179}`.
Its former width and height are retained; the view moves south by about 213 km.
At the view's horizontal centre the edges are approximately 58.21 N and
46.20 N. Albers map edges are not constant-latitude lines.

The northern reference is the existing BC Hydro `AllWatershedsUTM.shp`
feature with `WS_CODE=Williston`, transformed to EPSG:3005. Its northernmost
northing is 1,449,268.398692914 m. The display top is 20,000 m farther north.
This uses the watershed, rather than the reservoir's own shoreline.

## Existing-media implementation

The old BC prebuilt crop does not contain the new southern area. BC XL's
existing media does contain the entire target footprint, so the viewer uses
BC XL's original recipes, profile pointers, genuine manifest/product IDs,
and matching overlays as the media source for this selected BC view. The
selected region name, menu selection, saved preference and exported image
name remain BC. Storm tracks, webcams, dynamic imagery and the video are
all placed against the new geographic viewport.

The original weather pixels are cropped; no additional detail is invented.
This does not create a separate high-density regional encode. The BC XL
source's encoded pixel density and contour/symbol styling are retained.
The viewer automatically prefers native BC media if its published footprint
later contains the shifted target. Missing wide-source coverage retains the
original view rather than extrapolating absent video pixels.

No pipeline code, external data, caches, R2 objects, publisher schedules,
or BC XL display boundaries are changed. The older isolated backend crop
branch remains unactivated; it is no longer required to deliver this BC
display shift. Its proposed BC top should be revised to this measured
watershed-based position before any future native regional regeneration.

Verification: build/typecheck and 32 viewer tests, including source identity,
shared recipe/proxy selection, 20 km watershed clearance, retained footprint
aspect, safe fallback, future native-media preference and geographic video/
overlay placement. Published BC XL exact profiles for 3/6/12/24 hours and
their retained viewport were checked read-only. Interactive browser visual
verification was unavailable in this session.
