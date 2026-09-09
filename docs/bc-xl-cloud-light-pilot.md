# BC XL cloud-light pilot

Scope: `bc-large-overlay`, `eccc-geocolor`, live exact 3-hour and 6-hour
sidecars, including the operational default and both reusable weather cores.
Layered daylight, Soft colour, 50% depth. Other ranges/regions retain their
existing grade. No model training, network inference or generated image pixels.

The fixed Vivid baseline receives source-derived additive illumination and the
previously accepted monotone soft-colour response. Sun direction and elevation
use each observation's actual source time and the verified BC XL geographic
grid. Both the baseline grade and relief fade smoothly over solar elevation
0–12 degrees; supplied night imagery is retained at/below the horizon. This is
an appearance enhancement, not retrieved cloud height or physical shading.

## Efficiency and integrity

- Compile only potentially active neighbour constraints once, retaining exact
  reference traversal order. Zero shifts cannot become nonzero. This avoids
  repeatedly scanning source luminance and inactive pairs during relaxation.
- The optimized limiter matched the reference pixel-for-pixel on both full
  1920×1326 samples. Render/limiter phase fell from roughly 0.98–1.11 s to
  0.16–0.19 s in the comparison. The full Python/Node path (including Vivid,
  startup and byte transfer) measured 0.79–1.29 s across day/latest/dusk samples;
  cached PNG decode measured 0.047–0.052 s. These are local foreground timings,
  not a guarantee of launchd performance or total publication latency.
- Lossless satellite-layer cache is shared across loop ranges/presets. Keys
  include code/recipe/geography version, source stat identity and fetchedAt,
  source observation time, base identity, dimensions and crop.
- Cache lives below the existing unpublished `composite-frame-cache` and shares
  its 36-hour/6-GB pruning policy. 256 advisory lock buckets prevent duplicate
  enhancement work across concurrent workers without unbounded lock-file growth.
- The version also enters final-frame fingerprints and immutable manifests.
  Mixed 3/6/12-hour build requests split by style, so an entire generation uses
  one treatment. Overlays are composited after enhancement.
- A missing Node runtime, timeout, corrupt output or nonconvergent safeguard
  fails the new generation. Existing last-good pointers are retained. There is
  no per-frame fallback to the old grade. Atomic PNG writes prevent partial cache
  reads; corrupt cached satellite images are rebuilt.
- Node is resolved from RADARSAT_CLOUD_NODE, PATH, then the existing Homebrew
  location on this host. No runtime package installation is needed.

## Validation

38 video tests, 4 cloud cache/scope/night tests, 4 scheduler tests and 2 JavaScript
fidelity/equivalence tests passed. The isolated real-data cold build produced all
six variants without failures: 19 frames for 3 h and 37 for 6 h. Only 37 enhanced
satellite layers were created for 111 final composite frames. Day, dusk and night
checks retain protected details, alpha and required signed local contrast; the
night test is byte-identical to supplied imagery. Existing source GeoColor's own
VIS/IR transition is retained. Full seasonal/operational animation monitoring
remains useful; no new optical-flow or temporal synthesis is introduced.

Rollback: restore `radarsat/composite_video.py` and
`scripts/ops/run_video_scheduler.zsh` from the deployment backup at
`/tmp/bc-xl-cloud-pilot-deploy-backup`, then let the existing scheduler rebuild
and publish complete legacy loops. Do not combine styles inside a generation.

## Deployment result — 2026-09-09

The live initial build completed in 166.67 seconds with six successful profiles
and no failures. Public manifest, playlist and last-segment checks passed for
all six variants. The public catalog was verified to select the pilot style for
3/6 hours and legacy styles for 12/24 hours. Deployed style ID:
`layered-daylight-soft-50-v1-21abaeb7624473f8`.

Evidence: `/Users/greg/projects/fcstGraphics/experiments/imagery-beauty/operational-pilot/`.
