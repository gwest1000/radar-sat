"""Small cached geographic grids shared by exact and archive illumination."""
from functools import lru_cache
import json
from pathlib import Path

import numpy as np
from pyproj import Proj, Transformer

from .config import BC_XL_VIEWPORT, DOMAINS
from .geomet import projected_bbox


def geography(domain_id, viewport):
    return _geography(domain_id, tuple(float(viewport[k]) for k in ("left", "top", "width", "height")))


@lru_cache(maxsize=32)
def _geography(domain_id, viewport):
    # Preserve the accepted BC XL lighting exactly, including rounded nodes.
    if domain_id == "bc" and viewport == tuple(BC_XL_VIEWPORT[k] for k in ("left", "top", "width", "height")):
        return json.loads(Path(__file__).with_name("cloud_light").joinpath("geo.json").read_text())
    domain = DOMAINS[domain_id]
    xmin, ymin, xmax, ymax = projected_bbox(domain)
    left, top, width, height = viewport
    span_x, span_y = xmax - xmin, ymax - ymin
    x0, y1 = xmin + left * span_x, ymax - top * span_y
    gw, gh = (17, 13) if domain_id == "bc" else (65, 49)
    xs, ys = np.meshgrid(np.linspace(x0, x0 + width * span_x, gw),
                         np.linspace(y1, y1 - height * span_y, gh))
    inverse = Transformer.from_crs(domain.crs, "EPSG:4326", always_xy=True)
    forward = Transformer.from_crs("EPSG:4326", domain.crs, always_xy=True)
    lon, lat = inverse.transform(xs, ys)
    xlo, ylo = forward.transform(lon - .0001, lat)
    xhi, yhi = forward.transform(lon + .0001, lat)
    angle = np.arctan2(yhi - ylo, xhi - xlo)
    # The Pacific crosses 180 degrees: interpolate continuous longitudes,
    # otherwise nodes astride the dateline would light up as Greenwich.
    lon = np.rad2deg(np.unwrap(np.deg2rad(lon), axis=1))
    factors = Proj(domain.crs).get_factors(lon, lat)
    aspect = np.asarray(factors.parallel_scale) / np.asarray(factors.meridional_scale)
    grid = np.stack((lon, lat, angle), axis=-1)
    if not np.isfinite(grid).all() or not np.isfinite(aspect).all():
        raise ValueError("Invalid satellite illumination geography")
    return {"geoWidth": gw, "geoHeight": gh, "geo": grid.ravel().tolist(),
            "geoAspect": aspect.ravel().tolist()}
