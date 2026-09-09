# BC XL delivery compression trial

Selected pilot: H.264 CRF 22 for BC XL GeoColor live 3h/6h only.
User selected CRF 22 after reviewing the previews. Deployed and verified on all
six public BC XL 3h/6h profiles on 2026-09-09; the complete rebuild took 17.81 seconds.

Sequence: native satellite intake → existing reprojected WebP quality-88 archive
→ Vivid and Soft colour / 50% source-light treatment → lossless PNG cache
→ overlays → lossless composite PNG cache → one H.264 video encode → HLS delivery.
Changing video quality always re-encodes the lossless composite cache, never an
already compressed video. The existing original-image archive is retained;
feeding enhancement directly from a pre-WebP lossless source is a separate,
unimplemented quality/storage experiment. No additional lossy image stage was added.

37 identical enhanced frames, 1920×1326 content plus the 16-pixel clock strip,
through 2026-09-09 20:30:21 UTC. Default full-overlay 6-hour loop. CRF 20, 21,
and 22 use the same segmentation, frame durations, x264 preset and chroma format.
The three MP4 previews are remuxes of the test HLS, without another video encode.

| Setting | HLS media MB | Savings | Mean pure encode seconds |
|---|---:|---:|---:|
| CRF 20 baseline | 20.78 | — | 4.16 |
| CRF 21 comparison | 18.83 | 9.4% | 4.06 |
| CRF 22 selected | 17.02 | 18.1% | 4.00 |

Encoding timings are two repeats with rotated order and prepared input frames;
these exclude enhancement, catalog assembly and publication. Initial build timings
in results.json additionally include cache/playlist work and are not a pure encoder
comparison. There is little CPU saving; the main benefit is lower delivery/storage size.

Every source slot was decoded and checked against the alternating clock strip.
RGB fidelity measurements against the lossless composite cache are recorded in
results.json, with separate small-cloud and labels/radar regions. These include
common colour conversion and chroma subsampling and do not prove that lossy video
retains every tiny feature. CRF 21 was the conservative candidate; the user approved the CRF 22 appearance.

Open the three MP4s at native size. Compare fine cloud texture, gaps, thin coloured
contours, radar boundaries and label clarity during playback, not just stills.
The lossless PNG contact sheet compares two native-resolution crops of the final
frame. Production remains lossless between image-processing stages.
