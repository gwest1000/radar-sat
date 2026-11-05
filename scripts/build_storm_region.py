"""Rebuild the small BC+600 km storm-selection polygon from Natural Earth.

Natural Earth 1:10m admin-1 polygons (public domain), British Columbia.
https://www.naturalearthdata.com/downloads/10m-cultural-vectors/10m-admin-1-states-provinces/
EPSG:3005 metre-based buffer; simplify by 1 km before and after buffering.
Runtime and CI use the checked-in result, without Cartopy/downloads.
"""
import json
from pathlib import Path

from cartopy.io import shapereader
from pyproj import Transformer
from shapely.geometry import mapping
from shapely.ops import transform


def main():
    source = shapereader.natural_earth("10m", "cultural", "admin_1_states_provinces")
    record = next(r for r in shapereader.Reader(source).records()
                  if r.attributes.get("name") == "British Columbia"
                  and r.attributes.get("adm0_a3") == "CAN")
    project = Transformer.from_crs(4326, 3005, always_xy=True).transform
    region = transform(project, record.geometry).simplify(1000).buffer(600_000).simplify(1000)
    target = Path(__file__).resolve().parents[1] / "config" / "bc-storm-region.json"
    payload = {"type": "Feature", "properties": {
        "source": "Natural Earth 1:10m admin-1, British Columbia (public domain)",
        "sourceUrl": "https://www.naturalearthdata.com/downloads/10m-cultural-vectors/10m-admin-1-states-provinces/",
        "crs": "EPSG:3005", "bufferMetres": 600000, "simplifyMetres": 1000,
        "definition": "BC land area including islands, plus a 600 km outer buffer",
    }, "geometry": mapping(region)}
    target.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    print(f"Saved {target}: {target.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
