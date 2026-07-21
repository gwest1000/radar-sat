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
with conservative, source-aware alert thresholds. The interface treats
satellite as current/delayed at 45/75 minutes, radar at 15/30, precipitation
type at 20/35, and lightning at 25/45. This avoids reporting the normal
15–35-minute ECCC satellite publication delay as a local ingest outage.

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

The operational interface now concentrates on two complementary families:

1. **Overlay:** BC Large, tightly cropped BC Small, southwest, southeast and
   northeast views. Visible, infrared, day-visible/night-IR VisIR and the
   visible/IR-sandwich convective product are a mutually exclusive satellite
   group; radar and precipitation type are a second mutually exclusive group;
   lightning and wildfire hotspots can be switched independently. Coverage
   hatching follows the selected radar product. The chosen satellite clock
   anchors the loop; if no satellite is enabled, the selected radar/ptype,
   lightning or hotspot clock takes over.
2. **Snow/fog:** BC Small and the same three regional views. These use the
   qualitative day snow/fog/night-microphysics RGB with watershed and political
   boundaries.

The regional displays magnify normalized crops of the common BC grid rather
than duplicating frames in R2. This preserves the storage plan and avoids fake
upsampling: source ceilings remain 1 km for visible/radar/ptype, 2 km for IR,
and 2.5 km for lightning.

The hotspot layer archives a new snapshot every ten minutes from NRCan CWFIS's
documented public
[`hotspots_24h` WFS](https://cwfis.cfs.nrcan.gc.ca/downloads/docs/en/references/cwfif/cwfis-data-placemat.pdf),
restricted to detections assigned to BC. It
uses the record's observation timestamp rather than the download time: yellow
diamonds are 0–6 hours old, orange 6–12 hours and red 12–24 hours. Multiple
detections falling in one display pixel are consolidated. A detection is a
satellite thermal anomaly, not a confirmed wildfire or mapped fire perimeter.
If CWFIS is temporarily unavailable, core radar/satellite publication continues
and the last hotspot snapshot ages out rather than being silently restamped.

The watershed overlay uses the 54-polygon BC Hydro shapefile shared with the
forecast-model plots. It is transformed from WGS 84 / UTM zone 10N into the
common EPSG:3005 image grid and drawn with a dark halo and cyan centreline.

The standalone IR RGB is useful for cloud-top structure and motion but is not a
calibrated ABI Band 13 brightness-temperature field; the visible RGB is not a
calibrated reflectance grid. Numeric cold-cloud or reflectance thresholds would
require adding a raw NOAA ABI radiance/Cloud-and-Moisture-Imagery decoder.
Lightning-density transparency beyond 250 km of Canadian land or sea borders is
outside the CLDN mask and must not be read as confirmed zero lightning. Positive
2.5-km density cells are grouped into compact circular markers, not labelled as
individual strikes. New flashes use a magenta core, white ring and dark halo;
the core, ring and marker size fade through the 10–20 and 20–30-minute bins so
they remain visible over strong radar echoes.

### Satellite parallax

A physically defensible parallax correction is possible, but not from the
current rendered RGBs alone. It requires a per-pixel cloud-top height field and
the GOES-West viewing geometry, then relocates each cloudy pixel from its
apparent satellite line of sight to the corresponding surface intercept. NOAA's
ABI [cloud-top-height product](https://www.goes-r.gov/products/baseline-cloud-top-height-cloud-layer.html)
provides the needed height retrieval.

For GOES-West near 137.2°W, spherical line-of-sight geometry gives the following
representative apparent offsets across BC. The low end is approximately coastal
southwestern BC and the high end northern BC:

| Cloud-top height | Approximate BC offset |
|---:|---:|
| 1 km | 1.6–2.3 km |
| 3 km | 4.8–7.0 km |
| 6 km | 9.7–14 km |
| 10 km | 16–23 km |
| 15 km | 24–35 km |

The uncorrected cloud is normally plotted north to northeast of its true BC
position; correction moves it south to southwest, toward the satellite
subpoint. These estimates agree with NOAA's published GOES-West example at
Mount Adams, Washington—4.46 km at a 3.05-km cloud height and 22.22 km at
15.24 km ([Bernal Ayala et al., 2023](https://repository.library.noaa.gov/view/noaa/52613)).
A single fixed displacement would move low cloud, terrain and deep convection
by the same amount and would be more misleading than leaving the imagery
uncorrected. A future correction should ingest the height product, retain
quality flags, and mark any pixel lacking a valid height as uncorrected.

Radar uses ECCC's discrete authoritative legends. Rain thresholds are
`0.1, 1, 2, 4, 8, 12, 16, 24, 32, 50, 64, 100, 125, 200+ mm/h`; snow thresholds
are `0.1, 0.2, 0.3, 0.5, 0.75, 1, 1.5, 2, 3, 4, 5, 7.5, 10, 20+ cm/h`.
Transparent radar means no echo. A light grey one-pixel hatch marks no current
coverage and has been verified not to overlap valid ptype pixels.

Animation advances only after every raster in the next composition is loaded,
at a nominal 3.3 frames per second at 1× with a 1.2-second final-frame hold.
A slider provides 0.5×, 0.75×, 1×, 1.5×, 2×, 3×, 4× and 5×; product and layer
changes restart playback automatically. The viewer also provides keyboard
stepping, pause/play, 3/6/12/24-hour and 7-day ranges, UTC plus PDT/PST valid
time, and the actual SAT/RADAR/PTYPE/LTG/FIRE source times.

### CIRA SLIDER and public satellite data

The user's SLIDER link points to retired GOES-17; operational GOES-West is now
GOES-18. The imagery is publicly viewable and the
[SLIDER archive](https://slider-archive.cira.colostate.edu/) can download a
current frame or all loop frames as PNGs. CIRA offers excellent value-added
GeoColor, fog, nighttime-microphysics, convection and fire-temperature
imagery. Its tile/index layout is a display service, however, not a documented
or versioned production API. It is a valuable rendering reference and manual
backup, but a fragile primary ingest dependency. Public download access also
does not by itself establish blanket redistribution terms for every CIRA
derived product.

CIRA's product documentation explains the science recipes. In particular,
[GeoColor](https://rammb.cira.colostate.edu/ramsdis/online/product_descriptions.asp)
uses synthetic daytime true colour and a multispectral low-cloud/fog display at
night. We can create closely related products ourselves from the underlying
public measurements while retaining control of stretches, thresholds,
palettes, labels and projection.

### What is fixed today, and what raw data changes

The present satellite backgrounds are all **ECCC-rendered RGB images**, but
they are not all fetched from GeoMet during normal operation. The primary BC
path receives three-band GeoTIFFs from the ECCC Datamart AMQPS feed; GeoMet WMS
is the recent-gap fallback and the current broad-domain bootstrap. In both
cases ECCC has already converted the physical satellite channels into display
RGB values. Radar-Sat only reprojects and compresses them. A browser can alter
global brightness, contrast or saturation, but it cannot assign a defensible
colour to, for example, a 213 K cloud top because the delivered pixel no longer
contains calibrated brightness temperature. Even the standalone ECCC IR layer
therefore has a fixed upstream enhancement and no numerical colourbar.

Better configurable inputs are available:

| Source | Physical data and access | Cadence / resolution | Best use here |
|---|---|---|---|
| NOAA GOES-18 West and GOES-19 East | Anonymous public NODD object storage provides ABI L1b radiance and Level-2 CMI NetCDF. CMI contains reflectance factor for bands 1–6 and brightness temperature in kelvin for bands 7–16. | Full disk every 10 minutes; 0.5, 1 or 2 km at nadir by band | BC and a feathered two-satellite North America composite; GOES-18 also covers the eastern/central Pacific |
| NOAA mirror of JMA Himawari-9 | Anonymous public HSD files, segmented by band and latitude, in the [`noaa-himawari9` bucket](https://registry.opendata.aws/noaa-himawari/) | Full disk every 10 minutes; 0.5, 1 or 2 km at the sub-satellite point | Western/central Pacific and an overlap blend with GOES-18 |
| EUMETSAT MTG/FCI | Calibrated Level-1c through the [Data Store and EUMDAC API](https://user.eumetsat.int/data-access/data-store); EUMETView also supplies rendered WMS imagery | 10-minute full disk | Excellent Europe/Africa/eastern-Atlantic data, but it adds little useful geometry for BC or the Pacific |
| Polar orbiters (VIIRS/MODIS/Sentinel-3) | Public calibrated swaths and derived fire/smoke products | Much finer pixels but only a few useful passes per day | Optional high-detail smoke, snow and fire context; not a replacement for a 10-minute loop |

[NOAA's GOES open-data registry](https://registry.opendata.aws/noaa-goes/)
provides no-account access and says new data is added as it becomes available.
GOES-19 has been operational GOES-East since April 2025. A July 21 spot check
found representative 06:20 GOES-18/19 full-disk CMI objects published near
06:30, and 06:20 Himawari-9 segments near 06:30–06:31. Those are useful observed
lags, not an SLA. NOAA also distributes GOES through multiple NODD cloud
partners and NCEI/CLASS provides historical recovery; these are delivery
alternatives, although they share the same upstream satellite ground system.

For implementation, Level-2 CMI is the simplest source for custom single-band
visible, IR and water-vapour displays because the physical calibration is
already applied. Selected L1b bands are appropriate for multispectral RGBs.
[Satpy](https://satpy.readthedocs.io/en/stable/) can read ABI and Himawari HSD,
resample to our exact grids, build standard composites and apply our own
value-based colormaps. NOAA-supported
[Geo2Grid](https://www.ssec.wisc.edu/software/geo2grid/getting_started.html)
provides a useful operational reference and standard recipes including true
colour, natural colour, fog, night microphysics and day-severe-storms imagery.
Raw files should be transient local input: download only required bands or HSD
segments, render the retained WebP/PNG products, then delete the source files.
They do not need to consume R2 storage.

Raw ABI also makes a real parallax correction possible. NOAA's Level-2 cloud-top
height product can provide a height estimate for each cloudy pixel; that can be
combined with satellite viewing geometry before reprojection. The correction
would be labelled and quality-masked because cloud-top retrievals can fail or
represent only the uppermost layer.

## Broad-domain recommendation

Add the configurable source in three controlled stages while retaining the
ECCC RGBs as fallback:

1. **BC proof of quality:** clean-window IR brightness temperature with a
   labelled kelvin/°C enhancement, solar-corrected visible or true colour,
   standard snow/fog/night-microphysics, and day-convection RGB. Compare every
   product against CIRA SLIDER and the existing ECCC version.
2. **North America:** blend GOES-18 and GOES-19 by view angle rather than draw a
   hard seam. Publish at 30-minute live cadence and hourly after day one. Radar
   remains visible only where actual networks provide coverage.
3. **North Pacific:** blend GOES-18 with Himawari-9 in their broad overlap,
   again at 30-minute live and hourly archive cadence. Never imply radar over
   the open ocean.

At the planned 192 retained broad-domain times, each additional 0.5 MB rendered
layer costs about 96 MB per domain. Four satellite layers over both broad
domains would therefore add roughly 0.77 GB at that representative compression,
while the much larger raw downloads remain temporary. This preserves the
bucket guardrail and lets measured bandwidth, processing time and visual
quality decide whether more products are worth retaining.
