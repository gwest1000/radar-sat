# BC XL MSC GeoColour: operational enhancement

All prebuilt durations (3, 6, 12, 24 and 168 hours), all configured composite presets,
and archive satellite video with custom overlays use layered daylight, soft colour,
50% depth. Final H.264 uses CRF 22; cached enhanced and composed frames remain
lossless PNGs. Source WebP ingestion is unchanged.

The approved renderer and its style/cache version are unchanged. Eligibility
lives separately in cloud_policy.py so widening coverage does not invalidate
existing enhanced images. The legacy/archive builder uses the same BC XL crop
and canonical 1920-pixel enhancement cache as the exact sidecars; efficient
renditions are downsampled afterwards. Full-domain shared satellite media for
other products is not changed. Source timestamps still drive solar lighting
and the daylight/IR transition.

Exact sidecars own 3/6/12/24-hour composite presets. The legacy builder supplies
styled satellite media and 7-day composites,
without redundantly encoding the short/day exact presets. The existing catalog
retirement policy still hides redundant live/day legacy bundles; unsupported
custom live/day layer selections can use the existing image fallback. Both manifest types
carry satelliteStyle and videoEncoding metadata. The viewer does not apply
its legacy satellite CSS filter to enhanced video, replace its final frame
with an untreated raster, or reject an enhanced loop merely for trailing raw
imagery. A rendering failure leaves the previous complete profile available.

Validation: Python cloud-style/cache/legacy/archive integration tests, video
suite, scheduler tests; Node appearance/fidelity and viewer-policy tests;
production frontend build. First historical backfill costs more than regular
updates; all durations then share cached enhancement work.
