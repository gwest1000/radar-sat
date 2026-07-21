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
        crs="EPSG:3857",
        width=1600,
        height=900,
        tier="broad",
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
    "lightning-trail": Layer(
        id="lightning-trail",
        title="CLDN 30-minute age trail",
        source_layer=None,
        # Derived anchors may follow a six-minute radar clock. Keep alignment
        # tight so an age-coloured trail is not reused on much newer imagery.
        max_age_minutes=6,
    ),
    "site-radar": Layer(
        id="site-radar",
        title="BC site radar diagnostic",
        source_layer=None,
        role="background",
        max_age_minutes=20,
    ),
}


PRODUCTS: list[dict[str, object]] = [
    {
        "id": "bc-operations",
        "title": "BC Operations Mosaic",
        "shortTitle": "Operations",
        "group": "Situational",
        "domain": "bc",
        # Integrated products follow the ten-minute satellite clock.  Radar,
        # ptype, and lightning are selected at-or-before that observation so a
        # newer radar scan is never shown under an older satellite timestamp.
        "anchorLayer": "daynight",
        "defaultHours": 3,
        "description": "Continuous day/night satellite with radar rate, real coverage state, and a 30-minute lightning trail.",
        "layers": [
            {"id": "base-dark", "opacity": 1.0},
            {"id": "daynight", "opacity": 1.0, "optional": True, "defaultEnabled": True},
            {"id": "radar-coverage", "opacity": 1.0},
            {"id": "radar-rain", "opacity": 0.82, "optional": True, "defaultEnabled": True, "choiceGroup": "radar-rate"},
            {"id": "radar-snow", "opacity": 0.82, "optional": True, "defaultEnabled": False, "choiceGroup": "radar-rate"},
            {"id": "lightning-trail", "opacity": 1.0},
            {"id": "boundaries", "opacity": 1.0},
        ],
        "legends": ["radar-rain", "radar-snow", "lightning-age"],
        "notes": [
            "Cloud tops are not corrected for parallax.",
            "Hatched grey areas have no current radar coverage; transparent radar means no echo.",
        ],
    },
    {
        "id": "bc-radar",
        "title": "BC Radar Rate + Lightning",
        "shortTitle": "Radar",
        "group": "Radar",
        "domain": "bc",
        "anchorLayer": "radar-rain",
        "defaultHours": 3,
        "description": "Rain and snow precipitation rates on a neutral basemap, with an optional lightning trail.",
        "layers": [
            {"id": "base-dark", "opacity": 1.0},
            {"id": "radar-coverage", "opacity": 1.0},
            {"id": "radar-rain", "opacity": 0.95, "optional": True, "defaultEnabled": True, "choiceGroup": "radar-rate"},
            {"id": "radar-snow", "opacity": 0.95, "optional": True, "defaultEnabled": False, "choiceGroup": "radar-rate"},
            {"id": "lightning-trail", "opacity": 1.0, "optional": True},
            {"id": "boundaries", "opacity": 1.0},
        ],
        "legends": ["radar-rain", "radar-snow", "lightning-age"],
        "notes": ["Rates are DPQPE-derived composites, not base reflectivity."],
    },
    {
        "id": "bc-convective",
        "title": "BC Convective Satellite",
        "shortTitle": "Convection",
        "group": "Satellite",
        "domain": "bc",
        "anchorLayer": "convective",
        "defaultHours": 6,
        "description": "Visible/IR sandwich by day and night microphysics IR after dark, with lightning evolution.",
        "layers": [
            {"id": "convective", "opacity": 1.0},
            {"id": "lightning-trail", "opacity": 1.0},
            {"id": "boundaries", "opacity": 1.0},
        ],
        "legends": ["lightning-age"],
        "notes": ["Use cloud-top texture and cooling trends; do not infer a surface position without allowing for parallax."],
    },
    {
        "id": "bc-ptype",
        "title": "BC Surface Precipitation Type",
        "shortTitle": "Precip type",
        "group": "Radar",
        "domain": "bc",
        "anchorLayer": "ptype",
        "defaultHours": 6,
        "description": "Model-assisted radar surface precipitation type with its own coverage mask.",
        "layers": [
            {"id": "base-dark", "opacity": 1.0},
            {"id": "ptype-coverage", "opacity": 1.0},
            {"id": "ptype", "opacity": 0.90},
            {"id": "boundaries", "opacity": 1.0},
        ],
        "legends": ["ptype"],
        "notes": ["“Hail or rain” is not a definitive hail diagnosis. Do not compare this mosaic pixel-for-pixel with rate."],
    },
    {
        "id": "bc-lightning",
        "title": "BC Lightning Evolution",
        "shortTitle": "Lightning",
        "group": "Lightning",
        "domain": "bc",
        # Anchor the display clock to the ten-minute lightning interval rather
        # than the six-minute radar scan used to derive trail rasters. This
        # keeps VALID and the 0–10/10–20/20–30 minute age bins unambiguous.
        "anchorLayer": "lightning",
        "defaultHours": 6,
        "description": "Choose a three-interval age trail or the latest quantitative CLDN flash-density grid over subdued satellite.",
        "layers": [
            {"id": "daynight", "opacity": 0.64},
            {"id": "lightning-trail", "opacity": 1.0, "optional": True, "defaultEnabled": True, "choiceGroup": "lightning-view"},
            {"id": "lightning", "opacity": 1.0, "optional": True, "defaultEnabled": False, "choiceGroup": "lightning-view"},
            {"id": "boundaries", "opacity": 1.0},
        ],
        "legends": ["lightning-age", "lightning-density"],
        "notes": [
            "Density is a ten-minute accumulation normalized to flashes km⁻² min⁻¹; these are not individual strike data.",
            "Transparent pixels beyond 250 km of Canadian land or sea borders are outside the CLDN mask, not confirmed zero lightning.",
        ],
    },
    {
        "id": "bc-snowfog",
        "title": "BC Low Cloud / Snow–Fog",
        "shortTitle": "Snow / fog",
        "group": "Satellite",
        "domain": "bc",
        "anchorLayer": "snowfog",
        "defaultHours": 12,
        "description": "Snow/fog RGB by day and night microphysics after dark for low cloud and terrain obscuration.",
        "layers": [
            {"id": "snowfog", "opacity": 1.0},
            {"id": "boundaries", "opacity": 1.0},
        ],
        "legends": [],
        "notes": ["RGB colours are qualitative; no numerical colourbar applies."],
    },
    {
        "id": "bc-ir",
        "title": "BC Infrared Imagery",
        "shortTitle": "Infrared",
        "group": "Satellite",
        "domain": "bc",
        "anchorLayer": "ir",
        "defaultHours": 12,
        "description": "Around-the-clock GOES-West enhanced 2-km infrared RGB for cloud and weather-system structure and motion.",
        "layers": [
            {"id": "ir", "opacity": 1.0},
            {"id": "lightning-trail", "opacity": 1.0, "optional": True},
            {"id": "boundaries", "opacity": 1.0},
        ],
        "legends": ["lightning-age"],
        "notes": ["This public RGB is qualitative rather than calibrated ABI Band 13 brightness temperature; use trends, not numeric cloud-top thresholds."],
    },
    {
        "id": "bc-natural",
        "title": "BC Visible / Natural Colour",
        "shortTitle": "Visible",
        "group": "Satellite",
        "domain": "bc",
        "anchorLayer": "natural",
        "defaultHours": 12,
        "description": "Daytime natural-colour visible imagery for cloud texture, smoke, snow cover, and surface detail.",
        "layers": [
            {"id": "natural", "opacity": 1.0},
            {"id": "boundaries", "opacity": 1.0},
        ],
        "legends": [],
        "notes": ["Daylight only; the normal GOES-West overnight gap is not an outage."],
    },
    {
        "id": "bc-site-radar",
        "title": "BC Site Radar Diagnostic",
        "shortTitle": "Radar sites",
        "group": "Radar",
        "domain": "bc",
        "anchorLayer": "site-radar",
        "defaultHours": 3,
        "description": "Aldergrove, Halfmoon Peak, Silver Star Mountain, and Prince George DPQPE imagery.",
        "layers": [{"id": "site-radar", "opacity": 1.0}],
        "legends": [],
        "notes": ["Rendered DPQPE site imagery is base-like, but is not base reflectivity or radial velocity."],
    },
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
    "lightning-density": {
        "title": "Lightning flash density",
        "path": "static/legend-lightning-density.png",
    },
}
