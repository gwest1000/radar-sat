# Radar-Sat data, display, and storage assessment

_Assessment date: 2026-07-20. Storage values are planning estimates, not quotas._

## Bottom line

There is enough public data for high-quality operational BC loops. GOES-West
RGB imagery, the 1-km North American radar composite, 1-km precipitation type,
and 2.5-km lightning density align cleanly on a BC Albers grid. The main caveat
is “base radar”: the public station products are rendered DPQPE/CAPPI GIFs, not
raw base reflectivity or velocity. They are useful as diagnostic panels but
should not be labelled as base moments.

North America and the northeast Pacific are also practical at lower cadence.
Satellite coverage is good; radar remains limited to real radar-network
coverage, and there is no radar over the open Pacific. A truly trans-Pacific
view needs Himawari in addition to GOES-West.

## Feed assessment

| Feed | Useful resolution / cadence | Normal availability and lag | Source archive available to recovery jobs | Assessment |
|---|---|---|---|---|
| GOES-West visible / natural colour | 1 km nominal, 10 min | Daylight only; typically about 15–35 min behind valid time in the live audit | Datamart dated tree: currently 30 days | Excellent cloud texture, smoke and snow cover; no numerical colourbar |
| GOES-West IR and combined RGBs | 2 km native IR; combined products delivered on a 1-km grid, 10 min | Around the clock; typically 15–35 min lag | Datamart: 30 days | Excellent synoptic and convective context; high cloud has parallax |
| Radar rain / snow composite | 1 km, 6 min | Typically 6–15 min lag | GeoMet exposes only the last 3 hours | High quality for motion/rate; terrain blockage, range edges and mosaic seams remain |
| Surface precipitation type | 1 km, 6 min | Typically 10–20 min lag | GeoMet: last 3 hours | Valuable in winter, but model-assisted; do not compare pixel-for-pixel with rate |
| Station DPQPE / CAPPI | 480×480 map inside a 580×480 GIF, 6 min | Typically 6–15 min lag | Datamart: 30 days | Useful four-site diagnostic; not raw base reflectivity or radial velocity |
| CLDN lightning density | 2.5 km grid, ten-minute accumulation | Typically 10–25 min lag | Datamart: 30 days | Good storm-trend layer, not individual strikes; masked beyond 250 km from Canadian borders |

ECCC documents ten-minute satellite updates, 1/2-km product resolutions, and
day/night behaviour in its [GeoMet satellite catalogue](https://eccc-msc.github.io/open-data/msc-data/obs_satellite/readme_satellite_geomet_en/)
and [Datamart satellite guide](https://eccc-msc.github.io/open-data/msc-data/obs_satellite/readme_satellite-datamart_en/).
The radar composite is 1 km every six minutes with only three hours retained on
GeoMet ([radar guide](https://eccc-msc.github.io/open-data/msc-data/obs_radar/readme_radar_geomet_en/)).
CLDN is a ten-minute, approximately 2.5-km flash-density aggregation
([lightning guide](https://eccc-msc.github.io/open-data/msc-data/lightning/readme_lightning_en/)).
The dated Datamart tree currently retains 30 days
([Datamart guide](https://eccc-msc.github.io/open-data/msc-datamart/readme_en/)).

Lag is not a service guarantee. The ranges above combine the July 20 live audit
with conservative alert thresholds. Radar-Sat warns at 25/40 minutes for
satellite, 12/20 for radar, 18/30 for ptype, and 22/35 for lightning.

## Acquisition and reliability

- **Primary:** ECCC Datamart AMQPS for satellite GeoTIFFs, lightning GeoTIFFs,
  and the four station-radar streams. AMQPS announces each file and avoids
  polling. Live TLS/authentication and a temporary lightning binding were
  verified during setup.
- **GeoMet:** WMS is authoritative for composite rain/snow, precipitation type,
  and dynamic coverage, because no equivalent gridded Datamart product exists.
  In `auto` mode it can also fill recent satellite or lightning times missed by
  the native subscriber. Every request uses an explicit valid time and validates
  headers, content type, dimensions and blank imagery.
- **Recovery:** every three-minute job searches/backfills at least the recent
  three-hour window. Datamart products can be recovered from the dated tree;
  composite/ptype gaps older than three hours cannot.
- **Availability:** GeoMet and the primary Datamart are described as 24/7,
  although user support is best effort during business hours. The HPFX ECCC
  host is a useful high-bandwidth secondary, but ECCC says its Internet link
  lacks 24/7 redundancy.
- **Independent fallbacks:** NOAA public GOES S3 can replace satellite input
  after adding native ABI processing. NOAA MRMS can partially cover southern
  BC radar, but it is not a drop-in Canadian composite or ptype replacement.
  GLM is a different lightning sensor and has poor high-latitude BC geometry;
  an equivalent full-domain lightning backup would normally be commercial.

The current automatic fallback is native Datamart → GeoMet for recent satellite
and lightning gaps; it remains inside ECCC's failure domain. HPFX bindings and
NOAA decoders are documented secondary options, not yet automatic failover.
Health is written locally and does not send an external page or notification.

The weak point is the GeoMet-only three-hour radar window. An always-on Mac or
small server is much safer than scheduled GitHub Actions. The launch agents,
health checks, source-time validation, retry/backoff, atomic file writes, and
manifest-last R2 publication are designed around that constraint.

## Retention and R2 storage

The retention rule keeps every available observation for 24 hours and then
`:00`/`:30` observations through day seven. Counts therefore follow source
cadence rather than being uniform:

```text
10-minute satellite/lightning: 24 h × 6/h + 6 d × 48/d = 432
6-minute radar/ptype/site/trail: 24 h × 10/h + 6 d × 48/d = 528
```

The phase-two broad-domain target is 30-minute data for day one and hourly
thereafter, or 192 times per layer:

```text
24 h × 2/h + 6 d × 24/d = 192
```

That broad downsampling must be enabled with the phase-two ingest; the current
generic retention function alone would retain all source observations during
day one.

The July 20 BC sample at 1920×1472 measured 0.64–0.90 MB per 1-km satellite
WebP; the 2-km standalone IR product is another reusable background.
Radar, coverage, ptype and lightning PNGs were much smaller. That supports this
planning range:

| Archive component | Expected | Conservative stress case |
|---|---:|---:|
| Five BC satellite backgrounds | 1.65–2.00 GB | 3.00 GB |
| BC radar, coverage, ptype and lightning layers | 0.12–0.25 GB | 0.45 GB |
| Four-site montage, metadata and static assets | 0.10–0.25 GB | 0.35 GB |
| North America + Pacific lower-cadence layers | 0.35–0.70 GB | 0.90 GB |
| **New `radar-sat` bucket** | **2.2–3.2 GB** | **about 4.8 GB** |

Current account storage before Radar-Sat was approximately 1.15 GB, so the
expected account total is roughly 3.4–4.4 GB. This is comfortably below the
10 GB-month Standard-storage free allowance. Cloudflare defines that allowance
account-wide and bills average storage; it is not a hard bucket-size limit.
Standard storage is appropriate because it has no retrieval fee or minimum
duration. See [R2 pricing](https://developers.cloudflare.com/r2/pricing/).

Guardrails are deliberately lower than the allowance:

- warn at 4 GB in the `radar-sat` bucket;
- refuse growth above 5 GB;
- review Cloudflare's account-wide storage usage as well as the bucket guard;
- keep a 9-day `frames/` lifecycle rule as a failure backstop;
- upload reusable layers once and compose products in the browser;
- never upload native GOES GeoTIFFs or raw spool files.

The 4/5 GB publisher guard is a bucket-growth check, not an account-wide alert
service. Configure Cloudflare billing notifications separately. On the ingest
host, reserve roughly 8–10 GB free for the processed archive plus the local raw
spool and monitor both paths; the supplied local byte thresholds cover processed
output only.

## Display specification

The initial operational set is intentionally complementary:

1. **Operations mosaic:** day-visible/night-IR → no-coverage hatch → rain rate
   → lightning age → boundaries. This is the default situational display.
2. **Radar rate:** neutral basemap with mutually clear rain/snow controls and
   optional lightning. Rain and snow are not silently stacked.
3. **Convective satellite:** visible/IR sandwich by day, night microphysics
   after dark, with lightning but no filled radar competing with the RGB.
4. **Surface precipitation type:** ptype alone on a dark map with its own
   coverage mask; freezing rain and “hail or rain” retain ECCC wording.
5. **Lightning evolution:** choose an age trail (oldest orange, then cyan,
   current white) or quantitative ten-minute flash density using ECCC's
   `0–2+ flashes km⁻² min⁻¹` scale.
6. **Station radar diagnostic:** synchronized Aldergrove, Halfmoon Peak, Silver
   Star and Prince George panels, shown only after all four native streams have
   produced a usable time.
7. **Snow/fog**, **standalone 2-km IR**, and **visible/natural colour:**
   qualitative source RGBs, reprojected and display-compressed. IR can carry an
   optional lightning trail; visible is daylight-only.

The standalone IR RGB is useful for cloud-top structure and motion but is not a
calibrated ABI Band 13 brightness-temperature field; the visible RGB is not a
calibrated reflectance grid. Numeric cold-cloud or reflectance thresholds would
require adding a raw NOAA ABI radiance/Cloud-and-Moisture-Imagery decoder.
Lightning-density transparency beyond 250 km of Canadian land or sea borders is
outside the CLDN mask and must not be read as confirmed zero lightning.

Radar uses ECCC's discrete authoritative legends. Rain thresholds are
`0.1, 1, 2, 4, 8, 12, 16, 24, 32, 50, 64, 100, 125, 200+ mm/h`; snow thresholds
are `0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2, 3, 4, 5, 7.5, 10, 20+ cm/h`.
Transparent radar means no echo. A light grey one-pixel hatch marks no current
coverage and has been verified not to overlap valid ptype pixels.

Animation advances only after every raster in the next composition is loaded,
at a nominal 3.3 frames per second at 1× with a 1.2-second final-frame hold.
The viewer also provides 0.5×/2× speed choices, keyboard stepping, pause/play,
3/6/12/24-hour and 7-day ranges, UTC plus PDT/PST valid time, and the actual
SAT/RADAR/PTYPE/LTG source times. The integrated timeline is anchored to
satellite; other source observations must be at or before that time and inside
their age limit. In the Operations view the satellite is subdued while radar
is enabled and can be turned off entirely.

## Broad-domain recommendation

Add the broad views as phase two, after several days of BC reliability data:

- **North America:** feather GOES-West and GOES-East day/night imagery; overlay
  radar only where real coverage exists; 30-minute/one-hour retention.
- **North Pacific:** GOES-West IR for the northeast Pacific first, then add
  Himawari-9 for the western Pacific. Never imply ocean radar coverage.

This sequence preserves the highest-value BC products while keeping the bucket
well under its guardrail and exposes actual compression/traffic before adding a
second projection pipeline.
