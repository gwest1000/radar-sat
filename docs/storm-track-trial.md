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

Cyan paths show MSLP-based cyclones passing within 300 km of British Columbia,
including centres inside the province. The checked-in region includes islands
and is built from Natural Earth 1:10m admin-1 polygons (public domain), projected
to BC Albers (EPSG:3005), simplified by 1 km, buffered by 300,000 metres, then
simplified by 1 km again. This is a meteorological approximation rather than a
legal boundary or an exact geodesic-distance calculation. Rebuild with
`python -m scripts.build_storm_region` (Cartopy required only for rebuilding).
Track segments are checked for crossings between the native three-hour points;
entry ordering has three-hour precision. A faint dashed outline shows the
selection boundary. Entry does not by itself imply impacts in BC.
Dots carry UTC time and central pressure in hover tooltips. Dashed segments
are forecast relative to catalogue generation time. Solid segments are elapsed
model forecast positions, not observed tracks. Positions remain at their native
three-hour cadence. The overlay stays fixed at the latest RDPS forecast while
the historical radar/satellite loop plays, and explicitly says so on the map.
It adds no per-frame decoding, image caches, or playback animation callbacks.

Trial numbers are sorted by first entry into that envelope **within this model
run**. They can change between runs. They are not yet permanent weekly briefing identifiers.

## Weekly identifiers: proposed next stage

Use Monday 00:00 America/Vancouver through the following Monday, with a
week-qualified identifier (e.g. `2026-W44 #2`). Use entry into BC plus 300 km
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
