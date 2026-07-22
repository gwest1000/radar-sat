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
        width=1440,
        height=1080,
        tier="broad",
    ),
    "north-pacific": Domain(
        id="north-pacific",
        title="North Pacific",
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
    "daynight": Layer(
        id="daynight",
        title="GOES-West day visible / night IR",
        source_layer="GOES-West_1km_DayVis-NightIR",
        image_format="image/jpeg",
        extension="webp",
        role="background",
        max_age_minutes=40,
    ),
    "ir": Layer(
        id="ir",
        title="GOES-West enhanced infrared imagery",
        source_layer="GOES-West_2km_NightIR",
        image_format="image/jpeg",
        extension="webp",
        role="background",
        max_age_minutes=40,
    ),
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
        title="Radar no-coverage mask",
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
        title="Precipitation-type no-coverage mask",
        source_layer="Radar-Coverage_SfcPrecipType-Inverted",
        max_age_minutes=30,
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
        # Derived anchors may follow a six-minute radar clock. Keep alignment
        # tight so an age-coloured trail is not reused on much newer imagery.
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
        max_age_minutes=10,
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
        max_age_minutes=30,
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
    # Natural Earth BC bounds plus roughly 180 km of context on each side.
    "small": {"left": 0.2133, "top": 0.1239, "width": 0.6534, "height": 0.7500},
    "southwest": {"left": 0.3531, "top": 0.5300, "width": 0.3898, "height": 0.3438},
    "southeast": {"left": 0.5518, "top": 0.4854, "width": 0.3550, "height": 0.3473},
    "northeast": {"left": 0.4196, "top": 0.1525, "width": 0.4520, "height": 0.4422},
}


def _overlay_product(
    product_id: str,
    title: str,
    short_title: str,
    viewport: dict[str, float] | None = None,
) -> dict[str, object]:
    product: dict[str, object] = {
        "id": product_id,
        "title": title,
        "shortTitle": short_title,
        "group": "Overlay",
        "domain": "bc",
        "anchorLayer": "daynight",
        "defaultHours": 3,
        "description": (
            "A configurable satellite, radar or precipitation-type overlay with "
            "a 30-minute lightning trail, satellite wildfire hotspots and "
            "BC Hydro watershed boundaries."
        ),
        "layers": [
            {"id": "base-dark", "opacity": 1.0},
            {"id": "natural", "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite"},
            {"id": "ir", "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite"},
            {"id": "daynight", "opacity": 1.0, "optional": True, "defaultEnabled": True, "choiceGroup": "satellite"},
            {"id": "convective", "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite"},
            {"id": "raw-visir", "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite"},
            {"id": "raw-visible", "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite"},
            {"id": "raw-ir", "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite"},
            {"id": "smoke", "opacity": 1.0, "optional": True, "defaultEnabled": False},
            {"id": "radar-coverage", "opacity": 1.0, "enabledWith": "radar-rain"},
            {"id": "radar-rain", "opacity": 0.84, "optional": True, "defaultEnabled": True, "choiceGroup": "precipitation"},
            {"id": "ptype-coverage", "opacity": 1.0, "enabledWith": "ptype"},
            {"id": "ptype", "opacity": 0.90, "optional": True, "defaultEnabled": False, "choiceGroup": "precipitation"},
            {"id": "lightning-trail", "opacity": 1.0, "optional": True, "defaultEnabled": True},
            {"id": "hotspots", "opacity": 1.0, "optional": True, "defaultEnabled": True},
            {"id": "watersheds", "opacity": 1.0},
            {"id": "boundaries", "opacity": 1.0},
        ],
        "legends": ["radar-rain", "ptype", "lightning-age", "smoke-confidence", "hotspots", "watersheds"],
        "notes": [
            "Regional views magnify the shared aligned grid; source ceilings remain 1 km radar/visible, 2 km infrared and 2.5 km lightning without invented detail.",
            "Satellite cloud tops are not parallax-corrected because the RGB source does not contain per-pixel cloud height; deep cloud can appear 15–35 km north to northeast of its true BC position.",
            "The smoke tint marks NOAA ADP medium/high-confidence daytime clear-sky detections; transparency is not proof of smoke-free air and the colours do not represent concentration.",
            "Watersheds use the 54-polygon BC Hydro boundary source shared with the forecast-model plots.",
            "Wildfire hotspots are timestamped satellite thermal detections from the public NRCan CWFIS feed, not confirmed fire perimeters.",
        ],
    }
    if viewport is not None:
        product["viewport"] = viewport
    return product


def _snowfog_product(
    product_id: str,
    title: str,
    short_title: str,
    viewport: dict[str, float],
) -> dict[str, object]:
    return {
        "id": product_id,
        "title": title,
        "shortTitle": short_title,
        "group": "Snow / fog",
        "domain": "bc",
        "anchorLayer": "snowfog",
        "defaultHours": 12,
        "viewport": viewport,
        "description": "Snow/fog RGB by day and night microphysics after dark, with BC Hydro watershed boundaries.",
        "layers": [
            {"id": "snowfog", "opacity": 1.0},
            {"id": "watersheds", "opacity": 1.0},
            {"id": "boundaries", "opacity": 1.0},
        ],
        "legends": ["watersheds"],
        "notes": ["RGB colours are qualitative; no numerical colourbar applies."],
    }


def _broad_product(
    product_id: str,
    title: str,
    short_title: str,
    domain: str,
    description: str,
    notes: list[str],
) -> dict[str, object]:
    rapid_north_america = domain == "north-america"
    satellite_prefix = "westwx" if rapid_north_america else "raw"
    anchor_layer = f"{satellite_prefix}-ir"
    return {
        "id": product_id,
        "title": title,
        "shortTitle": short_title,
        "group": "Broad",
        "domain": domain,
        "anchorLayer": anchor_layer,
        "defaultHours": 24,
        "description": description,
        "layers": [
            {"id": "base-dark", "opacity": 1.0},
            {"id": f"{satellite_prefix}-visir", "opacity": 1.0, "optional": True, "defaultEnabled": True, "choiceGroup": "satellite"},
            {"id": f"{satellite_prefix}-visible", "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite"},
            {"id": anchor_layer, "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "satellite"},
            {"id": "smoke", "opacity": 1.0, "optional": True, "defaultEnabled": False},
            {"id": "radar-coverage", "opacity": 1.0, "enabledWith": "radar-rain"},
            {"id": "radar-rain", "opacity": 0.84, "optional": True, "defaultEnabled": True},
            {"id": "glm-lightning-trail", "opacity": 1.0, "optional": True, "defaultEnabled": True},
            {"id": "boundaries", "opacity": 1.0},
        ],
        "legends": [anchor_layer, "radar-rain", "glm-lightning-age", "smoke-confidence"],
        "notes": notes
        + [
            "Visible/IR uses a solar-elevation blend from calibrated true colour by day to neutral 10.3/10.4 µm infrared at night; no false-colour IR is mixed across the terminator.",
            *(
                ["North America satellite backgrounds use genuine GOES-18 scan times at a nominal ten-minute cadence; the far eastern edge is outside the best GOES-West viewing geometry."]
                if rapid_north_america
                else []
            ),
            "GLM symbols are optical total-lightning flash centroids, not ground-strike locations; useful GOES-18 coverage ends near 52°N.",
            "The smoke tint marks NOAA ADP medium/high-confidence daytime clear-sky detections; transparency is not proof of smoke-free air and the colours do not represent concentration.",
        ],
    }


PRODUCTS: list[dict[str, object]] = [
    _overlay_product("bc-large-overlay", "BC Large", "BC Large"),
    _overlay_product("bc-small-overlay", "BC Small", "BC Small", VIEWPORTS["small"]),
    _overlay_product("bc-southwest-overlay", "BC Southwest Overlay", "BC Southwest Overlay", VIEWPORTS["southwest"]),
    _overlay_product("bc-southeast-overlay", "BC Southeast Overlay", "BC Southeast Overlay", VIEWPORTS["southeast"]),
    _overlay_product("bc-northeast-overlay", "BC Northeast Overlay", "BC Northeast Overlay", VIEWPORTS["northeast"]),
    _snowfog_product("bc-small-snowfog", "BC Small Snow / Fog", "BC Snow / Fog", VIEWPORTS["small"]),
    _snowfog_product("bc-southwest-snowfog", "BC Southwest Snow / Fog", "SW Snow / Fog", VIEWPORTS["southwest"]),
    _snowfog_product("bc-southeast-snowfog", "BC Southeast Snow / Fog", "SE Snow / Fog", VIEWPORTS["southeast"]),
    _snowfog_product("bc-northeast-snowfog", "BC Northeast Snow / Fog", "NE Snow / Fog", VIEWPORTS["northeast"]),
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
    ),
    _broad_product(
        "north-pacific-overlay",
        "North Pacific Satellite / West Coast Radar",
        "North Pacific",
        "north-pacific",
        "Himawari-9/GOES-18 calibrated satellite imagery with real West Coast radar coverage.",
        [
            "Himawari-9 supplies the western Pacific and GOES-18 the eastern Pacific on a dateline-safe grid.",
            "There is no radar over the open ocean; hatching makes the available West Coast mosaic footprint explicit.",
        ],
    ),
]


LEGENDS: dict[str, dict[str, str]] = {
    "radar-rain": {
        "title": "Rain rate",
        "path": "static/legend-radar-rain.png",
    },
    "radar-snow": {
        "title": "Snow rate",
        "path": "static/legend-radar-snow.png",
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
    "hotspots": {
        "title": "Wildfire hotspot age",
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
