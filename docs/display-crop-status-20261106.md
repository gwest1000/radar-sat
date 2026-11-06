# Display update — 6 November 2026

Live-site changes: remove the storm-selection outline (keep the 600 km filter),
reduce the desktop rail from 260–300 px to 195–225 px, and crop E Pac/NA and
Pacific in presentation only. All tracks remain RDPS; per-view current-centre
filtering explains different track counts. ECMWF supplies broad-domain pressure
and height contours, not these tracks.

E Pac/NA targets 61.5 N–19.5 N and east to 66.9 W. Pacific targets a 61.5 N
northern edge while retaining the other edges. `app/display-viewport.ts` places
the retained encoded stage (including its transparent proxy surfaces) inside
the new viewport. It also places full-domain and pre-cropped image fallbacks
in the same coordinates. The main clock and interactive point overlays remain
in display space. No extra video assets, data downloads, publisher jobs, or
cadence changes are required. The crop is used only where the original catalog
and selected media contain it; unsupported media retain their original view.

BC/BC XL southwest shifts are implemented separately on local branch
`display-crops-20261106`, checkout
`/Users/greg/projects/radar-sat-display-crops-20261106`.
See its `docs/display-crop-rollout.md` for bounds, assumptions, tests, and
activation conditions. Its configuration is NOT active in production.
Those crops extend beyond existing encoded pixels and require guarded
regional raster/video regeneration. The owning pipeline's route checks found
so far verify directory existence, not the required volume UUID, filesystem,
reserve, and combined concurrent peak budget. No external writes or manual
regeneration were started. Greg/Codex must resolve that guard/budget gap and
drain affected readers/writers before activation; existing jobs remain intact.

Verification: site build/typecheck; 31 viewer tests including mathematical
source/proxy registration after cropping; two isolated geographic tests for
the staged backend bounds. Interactive browser visual verification remains
unavailable because browser automation is disabled on this machine.
