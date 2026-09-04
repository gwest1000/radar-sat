from __future__ import annotations

from dataclasses import dataclass


GEOMET_URL = "https://geo.weather.gc.ca/geomet"


@dataclass(frozen=True)
class Domain:
    id: str
    title: str
    west: float
    south: float
    east: float
    north: float
    crs: str
    width: int
    height: int
    tier: str
    projected_bounds: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class Layer:
    id: str
    title: str
    source_layer: str | None
    style: str = ""
    image_format: str = "image/png"
    extension: str = "png"
    role: str = "overlay"
    source: str = "ECCC GeoMet"
    max_age_minutes: int = 30
    daylight_only: bool = False
    point_schema: tuple[str, ...] = ()


DOMAINS: dict[str, Domain] = {
    "bc": Domain(
        id="bc",
        title="British Columbia and surroundings",
        west=-145.0,
        south=45.0,
        east=-108.0,
        north=63.0,
        crs="EPSG:3005",
        width=1920,
        height=1472,
        tier="bc",
        projected_bounds=(-550000.0, -100000.0, 2450000.0, 2200000.0),
    ),
    "north-america": Domain(
        id="north-america",
        title="North America",
        west=-180.0,
        south=5.0,
        east=-50.0,
        north=75.0,
        crs="EPSG:3857",
        # This overview is display-limited well before native ABI resolution.
        # A 1280 px grid reduces decode/transfer cost without losing useful
        # continental-scale meteorological detail.
        width=1280,
        height=960,
        tier="broad",
    ),
    "north-pacific": Domain(
        id="north-pacific",
        title="Pacific",
        west=120.0,
        south=5.0,
        east=-70.0,
        north=75.0,
        # Pacific-centred Mercator avoids cutting the map at the dateline.
        crs="EPSG:3832",
        width=1600,
        height=900,
        tier="broad",
        projected_bounds=(-3339584.7, 764000.0, 15584728.7, 11413000.0),
    ),
}


LAYERS: dict[str, Layer] = {
    "natural": Layer(
        id="natural",
        title="GOES-West natural colour",
        source_layer="GOES-West_1km_NaturalColor",
        image_format="image/jpeg",
        extension="webp",
        role="background",
        max_age_minutes=40,
        daylight_only=True,
    ),
    "convective": Layer(
        id="convective",
        title="GOES-West visible/IR sandwich",
        source_layer="GOES-West_1km_VisibleIRSandwich-NightMicrophysicsIR",
        image_format="image/jpeg",
        extension="webp",
        role="background",
        max_age_minutes=40,
    ),
    "snowfog": Layer(
        id="snowfog",
        title="GOES-West snow/fog and night microphysics",
        source_layer="GOES-West_1km_SnowFog-NightMicrophysics",
        image_format="image/jpeg",
        extension="webp",
        role="background",
        max_age_minutes=40,
    ),
    "eccc-geocolor": Layer(
        id="eccc-geocolor",
        title="MSC GOES-West GeoColor",
        source_layer=None,
        image_format="image/webp",
        extension="webp",
        role="background",
        source="ECCC Datamart",
        max_age_minutes=35,
    ),
    "radar-rain": Layer(
        id="radar-rain",
        title="Radar rain rate",
        source_layer="RADAR_1KM_RRAI",
        style="RADARURPPRECIPR14-LINEAR",
        max_age_minutes=20,
    ),
    "radar-snow": Layer(
        id="radar-snow",
        title="Radar snow rate",
        source_layer="RADAR_1KM_RSNO",
        style="RADARURPPRECIPS14-LINEAR",
        max_age_minutes=20,
    ),
    "radar-coverage": Layer(
        id="radar-coverage",
        title="Radar coverage mask",
        source_layer="RADAR_COVERAGE_RRAI.INV",
        max_age_minutes=20,
    ),
    "ptype": Layer(
        id="ptype",
        title="Surface precipitation type",
        source_layer="Radar_1km_SfcPrecipType",
        style="SfcPrecipType_Dis",
        max_age_minutes=30,
    ),
    "ptype-coverage": Layer(
        id="ptype-coverage",
        title="Precipitation-type coverage mask",
        source_layer="Radar-Coverage_SfcPrecipType-Inverted",
        max_age_minutes=30,
    ),
    "hrdps-hgt500": Layer(
        id="hrdps-hgt500",
        title="HRDPS 500 hPa geopotential height",
        source_layer=None,
        source="ECCC HRDPS Continental 2.5 km",
        max_age_minutes=90,
    ),
    "hrdps-mslp": Layer(
        id="hrdps-mslp",
        title="HRDPS mean sea-level pressure",
        source_layer=None,
        source="ECCC HRDPS Continental 2.5 km",
        max_age_minutes=90,
    ),
    "ecmwf-hgt500": Layer(
        id="ecmwf-hgt500",
        title="ECMWF control 500 hPa geopotential height",
        source_layer=None,
        source="ECMWF IFS Control",
        max_age_minutes=180,
    ),
    "ecmwf-mslp": Layer(
        id="ecmwf-mslp",
        title="ECMWF control mean sea-level pressure",
        source_layer=None,
        source="ECMWF IFS Control",
        max_age_minutes=180,
    ),
    "lightning": Layer(
        id="lightning",
        title="CLDN 10-minute flash density",
        source_layer="Lightning_2.5km_Density",
        style="Lightning",
        max_age_minutes=35,
    ),
    "lightning-points": Layer(
        id="lightning-points",
        title="CLDN 10-minute lightning-density display points",
        source_layer=None,
        image_format="application/json",
        extension="json",
        role="points",
        source="ECCC Datamart",
        max_age_minutes=35,
        point_schema=("x", "y", "ageMinutes", "count"),
    ),
    "lightning-trail": Layer(
        id="lightning-trail",
        title="CLDN 30-minute age trail",
        source_layer=None,
        # Lightning is a ten-minute observation. Retain the newest honest trail
        # through short source/ingest gaps instead of dropping it on intervening
        # five-minute display frames.
        max_age_minutes=30,
    ),
    "lightning-hour": Layer(
        id="lightning-hour",
        title="CLDN hourly lightning aggregate",
        source_layer=None,
        max_age_minutes=70,
    ),
    "lightning-flash": Layer(
        id="lightning-flash",
        title="CLDN newest-lightning arrival flash",
        source_layer=None,
        max_age_minutes=6,
    ),
    "glm-lightning": Layer(
        id="glm-lightning",
        title="GOES-18 GLM 10-minute total-lightning flashes",
        source_layer=None,
        source="NOAA GOES-18",
        max_age_minutes=20,
    ),
    "glm-lightning-trail": Layer(
        id="glm-lightning-trail",
        title="GOES-18 GLM 30-minute total-lightning age trail",
        source_layer=None,
        source="NOAA GOES-18",
        max_age_minutes=30,
    ),
    "glm-lightning-hour": Layer(
        id="glm-lightning-hour",
        title="GOES-18 GLM hourly total-lightning aggregate",
        source_layer=None,
        source="NOAA GOES-18",
        max_age_minutes=70,
    ),
    "glm-lightning-flash": Layer(
        id="glm-lightning-flash",
        title="GOES-18 GLM newest-lightning arrival flash",
        source_layer=None,
        source="NOAA GOES-18",
        max_age_minutes=10,
    ),
    "glm-lightning-live": Layer(
        id="glm-lightning-live",
        title="GOES-18 GLM rolling one-minute total-lightning overlay",
        source_layer=None,
        source="NOAA GOES-18",
        max_age_minutes=3,
    ),
    "glm-lightning-points": Layer(
        id="glm-lightning-points",
        title="GOES-18 GLM 10-minute total-lightning display points",
        source_layer=None,
        image_format="application/json",
        extension="json",
        role="points",
        source="NOAA GOES-18",
        max_age_minutes=20,
        point_schema=("x", "y", "ageMinutes", "count"),
    ),
    "smoke": Layer(
        id="smoke",
        title="GOES-18 ABI smoke detection",
        source_layer=None,
        source="NOAA GOES-18",
        max_age_minutes=40,
        daylight_only=True,
    ),
    "hotspots": Layer(
        id="hotspots",
        title="Satellite-detected wildfire hotspots",
        source_layer=None,
        source="NRCan CWFIS",
        # This raster also contains agency-reported active fires, which must
        # not blink off when the slower observation cycle misses a CWFIS
        # refresh. Its source timestamp remains visible in the map status.
        max_age_minutes=360,
    ),
    "hotspot-points": Layer(
        id="hotspot-points",
        title="Satellite-detected wildfire hotspot display points",
        source_layer=None,
        image_format="application/json",
        extension="json",
        role="points",
        source="NRCan CWFIS",
        max_age_minutes=30,
        point_schema=("x", "y", "ageMinutes", "frp", "count"),
    ),
    "active-fire-points": Layer(
        id="active-fire-points",
        title="Agency-reported active wildfire locations",
        source_layer=None,
        image_format="application/json",
        extension="json",
        role="points",
        source="NRCan CWFIS + BCWS + NIFC WFIGS",
        max_age_minutes=360,
        point_schema=(
            "x",
            "y",
            "statusAgeMinutes",
            "sizeHectares",
            "sourceCode",
            "highlightCode",
            "statusCode",
        ),
    ),
    "westwx-visir": Layer(
        id="westwx-visir",
        title="GOES-18 ten-minute true-colour / neutral infrared",
        source_layer=None,
        image_format="image/webp",
        extension="webp",
        role="background",
        source="NOAA GOES-18",
        max_age_minutes=25,
    ),
    "westwx-visible": Layer(
        id="westwx-visible",
        title="GOES-18 ten-minute calibrated true-colour satellite imagery",
        source_layer=None,
        image_format="image/webp",
        extension="webp",
        role="background",
        source="NOAA GOES-18",
        max_age_minutes=25,
        daylight_only=True,
    ),
    "westwx-ir": Layer(
        id="westwx-ir",
        title="GOES-18 ten-minute enhanced infrared",
        source_layer=None,
        image_format="image/webp",
        extension="webp",
        role="background",
        source="NOAA GOES-18",
        max_age_minutes=25,
    ),
    "raw-visible": Layer(
        id="raw-visible",
        title="Calibrated raw true-colour satellite imagery",
        source_layer=None,
        image_format="image/jpeg",
        extension="webp",
        role="background",
        source="NOAA Open Data",
        max_age_minutes=90,
        daylight_only=True,
    ),
    "raw-visir": Layer(
        id="raw-visir",
        title="True-colour visible / neutral infrared satellite imagery",
        source_layer=None,
        image_format="image/jpeg",
        extension="webp",
        role="background",
        source="NOAA Open Data",
        max_age_minutes=90,
    ),
    "raw-visir-5min": Layer(
        id="raw-visir-5min",
        title="GOES-18 five-minute PACUS true-colour / neutral infrared",
        source_layer=None,
        image_format="image/webp",
        extension="webp",
        role="background",
        source="NOAA GOES-18",
        max_age_minutes=15,
    ),
    "raw-visir-native": Layer(
        id="raw-visir-native",
        title="Full-resolution NOAA/CIRA GOES-18 GeoColor satellite imagery",
        source_layer=None,
        image_format="image/webp",
        extension="webp",
        role="background",
        source="NOAA/NESDIS/STAR",
        max_age_minutes=25,
    ),
    "raw-ir": Layer(
        id="raw-ir",
        title="Calibrated raw 10.3 µm brightness temperature",
        source_layer=None,
        image_format="image/jpeg",
        extension="webp",
        role="background",
        source="NOAA Open Data",
        max_age_minutes=90,
    ),
    "site-radar": Layer(
        id="site-radar",
        title="BC site radar diagnostic",
        source_layer=None,
        role="background",
        max_age_minutes=20,
    ),
}


VIEWPORTS: dict[str, dict[str, float]] = {
    # Normalized crops of the common EPSG:3005 BC grid. Reusing the same
    # aligned rasters gives regional displays without multiplying R2 storage.
    # A wider operational BC view. Relative to the previous crop, the added
    # context is weighted about 2:1 to the Pacific side while retaining enough
    # Alberta context for systems approaching from the east.
    # North reaches the projected position of BC's northeast corner at 60 N;
    # south bisects the Strait of Juan de Fuca between Vancouver Island and
    # the Olympic Mountains. East/west were symmetrically tightened to retain
    # the previous 1.506:1 display aspect ratio.
    "small": {"left": 0.164040, "top": 0.224890, "width": 0.670919, "height": 0.581179},
    "southwest": {"left": 0.3381, "top": 0.5300, "width": 0.4048, "height": 0.3438},
    "southeast": {"left": 0.5268, "top": 0.4854, "width": 0.4050, "height": 0.3473},
    "northeast": {"left": 0.3946, "top": 0.1525, "width": 0.5020, "height": 0.4422},
    # Southern Vancouver Island through Greater Vancouver and the Fraser
    # Valley. The projected aspect fills the map column on a typical 16:10
    # desktop after its fixed control rail is removed. The most recent two
    # expansions add only western context; the Fraser Valley and north/south
    # bounds remain fixed.
    "south-coast": {"left": 0.4923, "top": 0.6929, "width": 0.1612, "height": 0.1362},
}

# BC XL already uses the complete east/west source raster. A modest vertical
# trim lets that full retained width occupy more of the widescreen map stage
# without changing georegistration or invalidating the seven-day archive.
BC_XL_VIEWPORT = {"left": 0.0, "top": 0.0500, "width": 1.0, "height": 0.9000}


def regional_layer_id(base_layer_id: str, region_id: str) -> str:
    return f"{base_layer_id}-region-{region_id}"


for _region_id in VIEWPORTS:
    for _base_layer_id, _title, _max_age in (
        ("lightning-trail", "CLDN 30-minute age trail", 30),
        ("lightning-hour", "CLDN hourly lightning aggregate", 70),
        ("lightning-flash", "CLDN newest-lightning arrival flash", 6),
        ("glm-lightning-live", "GLM rolling one-minute lightning", 3),
        (
            "hotspots",
            "Active-wildfire and thermal-hotspot overlay",
            LAYERS["hotspots"].max_age_minutes,
        ),
    ):
        _layer_id = regional_layer_id(_base_layer_id, _region_id)
        LAYERS[_layer_id] = Layer(
            id=_layer_id,
            title=f"{_title} · {_region_id} crop",
            source_layer=None,
            source=(
                "NRCan CWFIS"
                if _base_layer_id == "hotspots"
                else "NOAA GOES-18"
                if _base_layer_id == "glm-lightning-live"
                else "ECCC GeoMet"
            ),
            max_age_minutes=_max_age,
        )
    for _base_layer_id, _title in (
        ("hrdps-hgt500", "HRDPS 500 hPa geopotential height"),
        ("hrdps-mslp", "HRDPS mean sea-level pressure"),
    ):
        _layer_id = regional_layer_id(_base_layer_id, _region_id)
        LAYERS[_layer_id] = Layer(
            id=_layer_id,
            title=f"{_title} · {_region_id} crop",
            source_layer=None,
            source="ECCC HRDPS Continental 2.5 km",
            max_age_minutes=90,
        )

# The South Coast radar is rendered directly on its display grid.  ECCC's
# 1-km continental rain-rate mosaic supplies complete coverage, while the
# public KATX/KLGX Level III DPR products replace it with higher-resolution
# dual-polarization rain rates where those U.S. radars have useful coverage.
_south_coast_radar_id = regional_layer_id("radar-rain", "south-coast")
LAYERS[_south_coast_radar_id] = Layer(
    id=_south_coast_radar_id,
    title="South Coast hybrid radar rain rate",
    source_layer=None,
    source="ECCC GeoMet + NOAA NEXRAD Level III",
    max_age_minutes=20,
)


BROAD_VIEWPORTS: dict[str, dict[str, float]] = {
    # 170 E–102 W, 20–66 N: the eastern half of the North Pacific through the
    # eastern edge of Colorado, without Kamchatka or the far tropical Pacific.
    "pacific-wna": {"left": 0.2100, "top": 0.1479, "width": 0.6500, "height": 0.7117},
    # Crop the continental display near 69 N and the eastern edge of Maine.
    # Keeping the source grid intact means satellite, radar and hazards remain
    # pixel-registered while the browser devotes its space to useful terrain.
    "north-america": {"left": 0.0, "top": 0.1763, "width": 0.8600, "height": 0.7800},
    # Retain the full Pacific western edge, remove the high Arctic above 69 N,
    # and stop at 120 W along the straight BC–Alberta boundary.
    "north-pacific": {"left": 0.0, "top": 0.075936, "width": 0.770000, "height": 0.900000},
}


def _overlay_product(
    product_id: str,
    title: str,
    short_title: str,
    viewport: dict[str, float] | None = None,
    *,
    five_minute: bool = False,
    max_hours: int | None = None,
) -> dict[str, object]:
    visir_layer = "raw-visir-5min" if five_minute else "raw-visir"
    product: dict[str, object] = {
        "id": product_id,
        "title": title,
        "shortTitle": short_title,
        "group": "Overlay",
        "domain": "bc",
        "anchorLayer": "eccc-geocolor",
        "frameIntervalMinutes": 10,
        "dayFrameIntervalMinutes": 30,
        "archiveFrameIntervalMinutes": 60,
        "defaultHours": 3,
        "description": (
            "A configurable satellite, radar or precipitation-type overlay with "
            "a 30-minute lightning trail, active wildfires, satellite thermal hotspots and "
            "BC Hydro watershed boundaries."
        ),
        "layers": [
            {"id": "base-dark", "opacity": 1.0},
            {"id": "eccc-geocolor", "opacity": 1.0, "optional": True, "defaultEnabled": True, "choiceGroup": "satellite", "controlSection": "regional-satellite"},
            {"id": visir_layer, "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite", "controlId": "noaa-visir"},
            {"id": "raw-ir", "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite", "controlId": "noaa-ir"},
            {"id": "convective", "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite", "controlSection": "regional-satellite"},
            {"id": "snowfog", "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite", "controlSection": "regional-satellite"},
            {"id": "smoke", "opacity": 1.0, "optional": True, "defaultEnabled": True},
            {"id": "radar-coverage", "opacity": 1.0, "enabledWith": "radar-rain"},
            {"id": "radar-rain", "opacity": 0.84, "optional": True, "defaultEnabled": True, "choiceGroup": "precipitation"},
            {"id": "ptype-coverage", "opacity": 1.0, "enabledWith": "ptype"},
            {"id": "ptype", "opacity": 0.90, "optional": True, "defaultEnabled": False, "choiceGroup": "precipitation"},
            {"id": "watersheds", "opacity": 1.0},
            {"id": "transmission-lines", "opacity": 1.0},
            {"id": "boundaries", "opacity": 1.0},
            {"id": "lightning-trail", "opacity": 1.0, "optional": True, "defaultEnabled": True, "controlId": "lightning"},
            {"id": "hotspots", "opacity": 1.0, "optional": True, "defaultEnabled": True},
            {"id": "model-mslp", "opacity": 1.0, "optional": True, "defaultEnabled": product_id != "bc-south-coast-overlay", "controlId": "model-contours"},
            {"id": "model-hgt500", "opacity": 1.0, "optional": True, "defaultEnabled": product_id != "bc-south-coast-overlay", "controlId": "model-contours"},
        ],
        "legends": ["radar-rain", "ptype", "lightning-age", "smoke-confidence", "hotspots", "watersheds", "transmission-lines"],
        "notes": [
            (
                "This view uses the one-kilometre MSC GeoColor product across BC. NOAA GeoColor is used only for a matching slot that is still missing when the display would otherwise be more than 35 minutes behind real time."
                if five_minute
                else "This view uses the one-kilometre MSC GeoColor product across BC. NOAA GeoColor is used only for a matching slot that is still missing when the display would otherwise be more than 35 minutes behind real time."
            ),
            "Satellite cloud tops are not parallax-corrected because the RGB source does not contain per-pixel cloud height; deep cloud can appear 15–35 km north to northeast of its true BC position.",
            "The smoke tint marks NOAA ADP low/medium/high-confidence daytime clear-sky detections; transparency is not proof of smoke-free air and the colours do not represent concentration.",
            "MSC GeoColor is the preferred one-kilometre, ten-minute BC satellite background. NOAA GeoColor fills a matching slot only after the 35-minute availability deadline; a later rebuild replaces that fallback with MSC when it arrives.",
            "Watersheds use the 54-polygon BC Hydro boundary source shared with the forecast-model plots.",
            "Transmission lines use the public GeoBC network shared with the forecast-model fire-weather plots.",
            "Filled coral flames are agency-reported active wildfires. Larger flames are official BCWS Wildfires of Note or, on the North America display, current U.S. ICS-209 large incidents; size alone does not enlarge an icon. Smaller hollow flames are timestamped NRCan CWFIS satellite thermal detections, not confirmed fire perimeters.",
        ],
    }
    if viewport is not None:
        product["viewport"] = viewport
    if max_hours is not None:
        product["maxHours"] = max_hours
    if product_id == "bc-south-coast-overlay":
        product["notes"].insert(
            1,
            "South Coast radar keeps the ECCC one-kilometre continental rain-rate mosaic as its complete-coverage base and overlays public 250-m-range-bin dual-polarization precipitation rates from KATX and KLGX where those U.S. radars can see.",
        )
    return product


def _broad_product(
    product_id: str,
    title: str,
    short_title: str,
    domain: str,
    description: str,
    notes: list[str],
    viewport: dict[str, float] | None = None,
) -> dict[str, object]:
    rapid_north_america = domain == "north-america"
    satellite_prefix = "westwx" if rapid_north_america else "raw"
    anchor_layer = f"{satellite_prefix}-ir"
    product: dict[str, object] = {
        "id": product_id,
        "title": title,
        "shortTitle": short_title,
        "group": "Broad",
        "domain": domain,
        "anchorLayer": anchor_layer,
        "frameIntervalMinutes": 20,
        "dayFrameIntervalMinutes": 30,
        "archiveFrameIntervalMinutes": 60,
        "defaultHours": 24,
        "description": description,
        "layers": [
            {"id": "base-dark", "opacity": 1.0},
            {"id": f"{satellite_prefix}-visir", "opacity": 1.0, "optional": True, "defaultEnabled": True, "choiceGroup": "satellite", "controlId": "noaa-visir"},
            {"id": anchor_layer, "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite", "controlId": "noaa-ir"},
            {"id": "smoke", "opacity": 1.0, "optional": True, "defaultEnabled": True},
            {"id": "radar-coverage", "opacity": 1.0, "enabledWith": "radar-rain"},
            {"id": "radar-rain", "opacity": 0.84, "optional": True, "defaultEnabled": True, "choiceGroup": "precipitation"},
            {"id": "ptype-coverage", "opacity": 1.0, "enabledWith": "ptype"},
            {"id": "ptype", "opacity": 0.90, "optional": True, "defaultEnabled": False, "choiceGroup": "precipitation"},
            {"id": "transmission-lines", "opacity": 1.0},
            {"id": "boundaries", "opacity": 1.0},
            {"id": "glm-lightning-trail", "opacity": 1.0, "optional": True, "defaultEnabled": True, "controlId": "lightning"},
            {"id": "lightning-trail", "opacity": 1.0, "enabledWith": "glm-lightning-trail"},
            {"id": "hotspots", "opacity": 1.0, "optional": True, "defaultEnabled": True},
            {"id": "model-mslp", "opacity": 1.0, "optional": True, "defaultEnabled": True, "controlId": "model-contours"},
            {"id": "model-hgt500", "opacity": 1.0, "optional": True, "defaultEnabled": True, "controlId": "model-contours"},
        ],
        "legends": [
            anchor_layer,
            "radar-rain",
            "ptype",
            "glm-lightning-age",
            "smoke-confidence",
            "hotspots",
            "transmission-lines",
        ],
        "notes": notes
        + [
            "Visible/IR uses a solar-elevation blend from calibrated true colour by day to neutral 10.3/10.4 µm infrared at night; no false-colour IR is mixed across the terminator.",
            *(
                ["North America satellite backgrounds use genuine GOES-18 scan times at a nominal ten-minute cadence; the far eastern edge is outside the best GOES-West viewing geometry."]
                if rapid_north_america
                else ["Pacific NOAA VIS/IR uses genuine GOES-18 full-disk scan times at a nominal ten-minute cadence; the half-hour Himawari-9/GOES-18 blend remains the infrared and availability fallback."]
            ),
            "GLM symbols are optical total-lightning flash centroids, not ground-strike locations; BC regional products use the ECCC/CLDN raster trail where GOES-18 GLM coverage is less useful.",
            "Agency-reported Canadian and U.S. active wildfire locations are shown separately from CWFIS satellite thermal detections.",
            "The smoke tint marks NOAA ADP low/medium/high-confidence daytime clear-sky detections; transparency is not proof of smoke-free air and the colours do not represent concentration.",
        ],
    }
    if viewport is not None:
        product["viewport"] = viewport
    return product


PRODUCTS: list[dict[str, object]] = [
    _overlay_product("bc-large-overlay", "BC XL", "BC XL", BC_XL_VIEWPORT),
    _overlay_product("bc-small-overlay", "BC", "BC", VIEWPORTS["small"], five_minute=True, max_hours=168),
    _overlay_product("bc-southwest-overlay", "BC Southwest", "BC SW", VIEWPORTS["southwest"], five_minute=True, max_hours=168),
    _overlay_product("bc-southeast-overlay", "BC Southeast", "BC SE", VIEWPORTS["southeast"], five_minute=True, max_hours=168),
    _overlay_product("bc-northeast-overlay", "BC Northeast", "BC NE", VIEWPORTS["northeast"], max_hours=168),
    _overlay_product(
        "bc-south-coast-overlay",
        "South Coast",
        "South Coast",
        VIEWPORTS["south-coast"],
        five_minute=True,
        max_hours=168,
    ),
    _broad_product(
        "pacific-wna-overlay",
        "Eastern Pacific / Western North America",
        "Pacific/WNA",
        "north-pacific",
        "A focused Eastern Pacific and Western North America satellite view with real West Coast radar coverage.",
        [
            "The Pacific-centred crop covers roughly 170°E–102°W and 20–66°N without a dateline seam.",
            "There is no radar over the open ocean; hatching makes the available West Coast mosaic footprint explicit.",
        ],
        BROAD_VIEWPORTS["pacific-wna"],
    ),
    _broad_product(
        "north-america-overlay",
        "North America Satellite / Radar",
        "North America",
        "north-america",
        "Ten-minute GOES-18 calibrated satellite imagery with the ECCC continental radar composite.",
        [
            "GOES-18 supplies genuine ten-minute scan times on the common 2 km display grid; the far eastern edge has weaker viewing geometry than the legacy GOES-18/19 blend.",
            "Radar is observed only where the ECCC continental mosaic has coverage; hatching marks the remainder.",
        ],
        BROAD_VIEWPORTS["north-america"],
    ),
    _broad_product(
        "north-pacific-overlay",
        "Pacific Satellite / West Coast Radar",
        "Pacific",
        "north-pacific",
        "Ten-minute NOAA GOES-18 GeoColor imagery with real West Coast radar coverage.",
        [
            "NOAA GOES-18 GeoColor supplies the ten-minute VIS/IR clock; the slower Himawari-9/GOES-18 blend remains available for infrared and fallback coverage.",
            "There is no radar over the open ocean; hatching makes the available West Coast mosaic footprint explicit.",
        ],
        BROAD_VIEWPORTS["north-pacific"],
    ),
]

# Exact-range, fully composited playback is generated from this operational
# policy instead of a hard-coded two-domain pilot.  Satellite IDs are supplied
# by the active profile (MSC GeoColor for the BC family, NOAA for broad views);
# the entries below name only optional overlay controllers.
VIDEO_EXACT_RANGES: dict[str, tuple[int, ...]] = {
    "bc-small-overlay": (3, 6, 12, 24),
    "bc-southwest-overlay": (3, 6, 12, 24),
    "bc-southeast-overlay": (3, 6, 12, 24),
    "bc-northeast-overlay": (3, 6, 12, 24),
    "bc-large-overlay": (3, 6, 12, 24),
    "bc-south-coast-overlay": (3, 6, 12),
    "pacific-wna-overlay": (12, 24),
    "north-america-overlay": (12, 24),
    "north-pacific-overlay": (12, 24),
}

VIDEO_ARCHIVE_PRODUCTS = frozenset(
    {
        "bc-large-overlay",
        "pacific-wna-overlay",
        "north-america-overlay",
        "north-pacific-overlay",
    }
)

VIDEO_TRACKS_BY_PRODUCT: dict[str, tuple[str, ...]] = {
    product_id: (
        "live",
        *(("day",) if 24 in ranges else ()),
        *(("archive",) if product_id in VIDEO_ARCHIVE_PRODUCTS else ()),
    )
    for product_id, ranges in VIDEO_EXACT_RANGES.items()
}


def _default_video_optional_layers(product_id: str) -> tuple[str, ...]:
    """Return the product's real UI-default optional controllers.

    Satellite choice-group members are supplied by the video profile itself,
    while ``enabledWith`` companions (for example radar coverage) are resolved
    by the composite recipe builder. Keeping this derived from ``PRODUCTS``
    prevents the low-power default video from silently drifting away from the
    controls a new browser session actually enables.
    """
    product = next(value for value in PRODUCTS if value.get("id") == product_id)
    return tuple(
        str(layer["id"])
        for layer in product.get("layers", ())
        if layer.get("optional")
        and layer.get("defaultEnabled")
        and not layer.get("enabledWith")
        and layer.get("choiceGroup") != "satellite"
    )


VIDEO_COMPOSITE_PRESETS: dict[str, tuple[dict[str, object], ...]] = {
    product_id: (
        {
            "id": "operational-default-v1",
            "optionalLayers": _default_video_optional_layers(product_id),
        },
    )
    for product_id in VIDEO_EXACT_RANGES
}

# A reusable opaque prefix is intentionally much narrower than the exact
# composite matrix while the browser and storage costs are measured.  The
# prefix ends at the static linework; every eligible layer is therefore above
# the H.264 plane and can be added without changing the recipe's visual order.
VIDEO_HYBRID_CORE_PRODUCTS = frozenset(
    {
        "bc-large-overlay",
        "bc-northeast-overlay",
        "north-america-overlay",
    }
)
VIDEO_SMOKE_CORE_PRODUCTS = VIDEO_HYBRID_CORE_PRODUCTS
for _product_id in VIDEO_HYBRID_CORE_PRODUCTS:
    _lightning_id = (
        "lightning-trail"
        if _product_id.startswith("bc-")
        else "glm-lightning-trail"
    )
    VIDEO_COMPOSITE_PRESETS[_product_id] = (
        *VIDEO_COMPOSITE_PRESETS[_product_id],
        {
            "id": "weather-smoke-core-v1",
            "compositeKind": "hybrid-prefix",
            "optionalLayers": ("smoke", "radar-rain"),
            "overlayLayers": (
                _lightning_id,
                "hotspots",
                "model-mslp",
                "model-hgt500",
            ),
        },
        {
            "id": "weather-core-v1",
            "compositeKind": "hybrid-prefix",
            "optionalLayers": ("radar-rain",),
            "overlayLayers": (
                _lightning_id,
                "hotspots",
                "model-mslp",
                "model-hgt500",
            ),
        },
    )

# South Coast uses one regular ten-minute prebuilt loop. Optional model
# contours remain available through the browser-composited fallback without
# doubling the operational video workload.
VIDEO_COMPOSITE_PRESETS["bc-south-coast-overlay"] = (
    {
        "id": "operational-default-v1",
        "optionalLayers": _default_video_optional_layers("bc-south-coast-overlay"),
    },
)


def _video_composite_preset(
    product_id: str,
    preset_id: str,
) -> dict[str, object] | None:
    return next(
        (
            value
            for value in VIDEO_COMPOSITE_PRESETS.get(product_id, ())
            if value.get("id") == preset_id
        ),
        None,
    )


def _resolved_video_layer_ids(
    product_id: str,
    satellite_layer_id: str,
    optional_layer_ids: tuple[str, ...],
) -> tuple[str, ...]:
    product = next(
        (value for value in PRODUCTS if value.get("id") == product_id),
        None,
    )
    if product is None:
        return ()
    requested = set(optional_layer_ids)
    selected: list[str] = []
    for recipe in product.get("layers", ()):
        recipe_id = str(recipe.get("id", ""))
        if not recipe_id:
            continue
        enabled_with = str(recipe.get("enabledWith", ""))
        if enabled_with:
            if enabled_with in requested:
                selected.append(recipe_id)
            continue
        if recipe.get("choiceGroup") == "satellite":
            if recipe_id == satellite_layer_id:
                selected.append(recipe_id)
            continue
        if recipe.get("optional") and recipe_id not in requested:
            continue
        selected.append(recipe_id)
    return tuple(selected)


def video_composite_kind(product_id: str, preset_id: str) -> str:
    """Return ``exact`` or the canonical reusable composite kind."""
    preset = _video_composite_preset(product_id, preset_id)
    if preset is None:
        return ""
    return str(preset.get("compositeKind", "exact"))


def video_composite_layer_ids(
    product_id: str,
    satellite_layer_id: str,
    preset_id: str,
) -> tuple[str, ...]:
    """Resolve the one canonical recipe stack for a named video preset."""
    preset = _video_composite_preset(product_id, preset_id)
    if preset is None:
        return ()
    return _resolved_video_layer_ids(
        product_id,
        satellite_layer_id,
        tuple(str(value) for value in preset.get("optionalLayers", ())),
    )


def video_composite_overlay_layer_ids(
    product_id: str,
    satellite_layer_id: str,
    preset_id: str,
) -> tuple[str, ...]:
    """Return the ordered dynamic suffix supported by a hybrid prefix.

    This resolves ``enabledWith`` companions (notably the Canadian lightning
    trail on broad GLM views) and verifies that the baked stack is an exact
    prefix of the combined recipe.  Returning no values for an invalid policy
    prevents a misconfigured opaque video from covering an intended underlay.
    """
    preset = _video_composite_preset(product_id, preset_id)
    if preset is None or video_composite_kind(product_id, preset_id) != "hybrid-prefix":
        return ()
    baked = video_composite_layer_ids(product_id, satellite_layer_id, preset_id)
    combined_optional = tuple(
        dict.fromkeys(
            (
                *(str(value) for value in preset.get("optionalLayers", ())),
                *(str(value) for value in preset.get("overlayLayers", ())),
            )
        )
    )
    combined = _resolved_video_layer_ids(
        product_id,
        satellite_layer_id,
        combined_optional,
    )
    if combined[: len(baked)] != baked:
        return ()
    return combined[len(baked) :]


LEGENDS: dict[str, dict[str, str]] = {
    "radar-rain": {
        "title": "Rain rate",
        "path": "static/legend-radar-rain.png",
    },
    "ptype": {
        "title": "Surface precipitation type",
        "path": "static/legend-ptype.png",
    },
    "lightning-age": {
        "title": "Lightning age",
        "kind": "lightning-age",
    },
    "glm-lightning-age": {
        "title": "GLM total-lightning age",
        "kind": "lightning-age",
    },
    "smoke-confidence": {
        "title": "Satellite smoke detection confidence",
        "kind": "smoke-confidence",
    },
    "lightning-density": {
        "title": "Lightning flash density",
        "path": "static/legend-lightning-density.png",
    },
    "watersheds": {
        "title": "BC Hydro watershed boundary",
        "kind": "watersheds",
    },
    "transmission-lines": {
        "title": "BC transmission lines",
        "kind": "transmission-lines",
    },
    "model-hgt500": {
        "title": "500 hPa height",
        "kind": "model-hgt500",
    },
    "model-mslp": {
        "title": "Mean sea-level pressure",
        "kind": "model-mslp",
    },
    "hotspots": {
        "title": "Active wildfires and thermal hotspots",
        "kind": "hotspots",
    },
    "raw-ir": {
        "title": "10.3 µm cloud-top temperature",
        "kind": "raw-ir",
    },
    "westwx-ir": {
        "title": "10.3 µm cloud-top temperature",
        "kind": "raw-ir",
    },
}
