# Storm-track trial

RDPS mean-sea-level-pressure cyclone tracks are the initial experimental source.
The track model must be identified independently of the HRDPS/ECMWF contour model.

## Local trial

Run `.venv/bin/python -m scripts.update_storm_tracks` from the repository root,
then build/serve the site as usual. Enable **RDPS storm tracks (trial)** under
Layers. This generates only `public/storm-tracks.json`; no video rebuild or R2
publication is performed by this command. The fetcher selects a complete 0–84-hour run at
three-hour intervals, with four bounded download workers. Failed downloads
leave the previous catalogue intact. The Pages workflow refreshes this small
catalogue on deployment and at 03:37, 09:37, 15:37, and 21:37 UTC each day.
An upstream outage preserves the checked-in snapshot, with an explicit stale
label when its model run is over 12 hours old. Open, visible viewers recheck
the small catalogue every 30 minutes; hidden viewers suspend those checks.

Cyan paths show MSLP-based cyclones passing within 600 km of British Columbia,
including centres inside the province. The checked-in region includes islands
and is built from Natural Earth 1:10m admin-1 polygons (public domain), projected
to BC Albers (EPSG:3005), simplified by 1 km, buffered by 600,000 metres, then
simplified by 1 km again. This is a meteorological approximation rather than a
legal boundary or an exact geodesic-distance calculation. Rebuild with
`python -m scripts.build_storm_region` (Cartopy required only for rebuilding).
Track segments are checked for crossings between the native three-hour points;
entry ordering has three-hour precision. The selection region is used only for
filtering and is not drawn on the map.
Entry does not by itself imply impacts in BC. The initial
300 km cutoff excluded the 30 October 2026 offshore-Vancouver-Island low:
its 18 UTC forecast position was about 332 km from BC and its forecast closest
approach about 328 km. The subsequent 400 km threshold included that low.
On 5 November 2026 the user requested 600 km to include a wider area of BC-relevant
offshore systems. Selection still uses closest approach anywhere along the
forecast track; it is not a requirement for the centre to be within 600 km now.
The separate current-centre-in-viewport filter below still applies.
Dots carry PCT time and central pressure in hover tooltips. Weekday/hour labels
(e.g. `Fri 11 PCT`) use three-letter days and are placed at 24-hour intervals from the first track point,
plus the final native point so tracks shorter than a day have both endpoints
labelled. These labels omit the calendar date and minutes. An arrowhead
marks the final moving segment. The map caption is “Beta: RDPS storm tracks”;
hover over it for the forecast run and selection details. New/unspecified layer
preferences enable this layer by default; an explicit saved off choice persists.
Dashed segments
are forecast relative to catalogue generation time. Solid segments are elapsed
model forecast positions, not observed tracks. Positions remain at their native
three-hour cadence. The overlay stays fixed at the latest RDPS forecast while
the historical radar/satellite loop plays, and explicitly says so on the map.
Only storms whose predicted centres are currently inside the selected map's
cropped viewport are displayed. The current time is sampled when the catalogue
is fetched (including its 30-minute refresh); it does not follow the historical
animation clock. A storm forming later or forecast to enter this view later is
excluded until present. Three-hourly source positions bracket the visibility
check, with linear interpolation used only for that check; no extra track dots
or weather frames are generated, and missing source slots are not bridged.
The entire available predicted path remains visible for an included storm,
clipped to the map. Local track numbers are preserved when others are hidden.
It adds no per-frame decoding, image caches, or playback animation callbacks.

The main map clock and storm labels use BC's permanent UTC-7 Pacific Time
(`PCT`) from 8 March 2026 onward, even on browsers with older timezone data.
Earlier historical timestamps retain their applicable Vancouver time-zone rule.
Official terminology: https://news.gov.bc.ca/releases/2026CITZ0009-001073

Trial numbers are sorted by first entry into that envelope **within this model
run**. They can change between runs. They are not yet permanent weekly briefing identifiers.

## Weekly identifiers: proposed next stage

Use Monday 00:00 America/Vancouver through the following Monday, with a
week-qualified identifier (e.g. `2026-W44 #2`). Use entry into BC plus 600 km
as the arrival rule.
Maintain a persistent identity registry, match successive forecasts at common
valid times using track distance and motion, and leave ambiguous split/merge
matches for review rather than silently renaming systems. ECCC disturbance IDs
include model-run information and are not suitable as cross-run identifiers.
Assign provisional arrival order to forecasts; freeze briefing numbers once
issued, even if later forecasts change arrival order. Systems carried over
Monday retain their original week-qualified identifier. Starting midweek must
be marked partial unless earlier runs have been reconstructed.

Start with RDPS MSLP tracks over HRDPS contours. GDPS MSLP tracks would be the
next broad-domain comparison over ECMWF; label the differing model sources.
GEPS is better reserved for a separate uncertainty display to avoid a cluttered
ensemble spaghetti overlay and duplicate storm numbering across models.

Source: https://eccc-msc.github.io/open-data/msc-data/nwp_rdps/readme_rdps-stormtrack-datamart_en/
