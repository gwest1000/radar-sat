from __future__ import annotations

import io
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .config import Domain
from .geomet import projected_bbox


DEFAULT_BCH_WATERSHEDS = (
    Path(__file__).resolve().parents[2]
    / "fcstGraphics"
    / "data"
    / "bc_watersheds"
    / "bch"
    / "AllWatershedsUTM.shp"
)


def bch_watershed_source() -> Path:
    configured = os.environ.get("RADARSAT_BCH_WATERSHEDS")
    return Path(configured).expanduser() if configured else DEFAULT_BCH_WATERSHEDS


def save_satellite(content: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = Image.open(io.BytesIO(content)).convert("RGB")
    image.save(destination, "WEBP", quality=88, method=4)


def save_overlay(content: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = Image.open(io.BytesIO(content)).convert("RGBA")
    image = image.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    image.save(destination, "PNG", optimize=True)


def save_coverage(content: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = Image.open(io.BytesIO(content)).convert("RGBA")
    array = np.asarray(image).copy()
    # GeoMet's ``*.INV``/``*-Inverted`` layers already paint the area with no
    # radar estimate. Preserve that semantic: hatch their non-transparent
    # pixels and leave valid radar footprints clear.
    mask = array[:, :, 3] > 20
    y, x = np.indices(mask.shape)
    hatch = ((x + y) % 10) < 1
    array[:, :, :3] = np.where(mask[:, :, None], np.array([151, 160, 170], dtype=np.uint8), 0)
    array[:, :, 3] = np.where(mask, np.where(hatch, 78, 30), 0).astype(np.uint8)
    output = Image.fromarray(array).quantize(
        colors=16,
        method=Image.Quantize.FASTOCTREE,
        dither=Image.Dither.NONE,
    )
    output.save(destination, "PNG", optimize=True)


def reproject_overlay(
    content: bytes,
    source_domain: Domain,
    target_domain: Domain,
    *,
    outside_no_coverage: bool = False,
) -> bytes:
    """Warp a transparent WMS raster to a target grid unsupported by GeoMet."""
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject

    source = np.asarray(Image.open(io.BytesIO(content)).convert("RGBA"))
    destination = np.zeros((target_domain.height, target_domain.width, 4), dtype=np.uint8)
    source_bounds = projected_bbox(source_domain)
    target_bounds = projected_bbox(target_domain)
    source_transform = from_bounds(*source_bounds, source_domain.width, source_domain.height)
    target_transform = from_bounds(*target_bounds, target_domain.width, target_domain.height)
    for channel in range(4):
        reproject(
            source=source[:, :, channel],
            destination=destination[:, :, channel],
            src_transform=source_transform,
            src_crs=source_domain.crs,
            dst_transform=target_transform,
            dst_crs=target_domain.crs,
            resampling=Resampling.nearest,
            src_nodata=0,
            dst_nodata=None,
            init_dest_nodata=False,
        )
    if outside_no_coverage:
        footprint = np.zeros((target_domain.height, target_domain.width), dtype=np.uint8)
        reproject(
            source=np.ones(source.shape[:2], dtype=np.uint8),
            destination=footprint,
            src_transform=source_transform,
            src_crs=source_domain.crs,
            dst_transform=target_transform,
            dst_crs=target_domain.crs,
            resampling=Resampling.nearest,
            src_nodata=None,
            dst_nodata=0,
        )
        destination[footprint == 0] = np.array((181, 181, 181, 128), dtype=np.uint8)
    output = io.BytesIO()
    Image.fromarray(destination, "RGBA").save(output, "PNG", optimize=True)
    return output.getvalue()


def lightning_trail(source_paths: list[Path | None], destination: Path) -> None:
    """Render age-fading lightning clusters as haloed circular flash markers."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    size: tuple[int, int] | None = None
    masks: list[np.ndarray | None] = []
    for path in source_paths:
        if path is None or not path.exists():
            masks.append(None)
            continue
        source = Image.open(path).convert("RGBA")
        size = source.size
        masks.append(np.asarray(source.getchannel("A")) > 20)
    if size is None:
        raise ValueError("At least one lightning source frame is required")

    def component_centres(mask: np.ndarray) -> list[tuple[int, int, int]]:
        active = {(int(y), int(x)) for y, x in np.argwhere(mask)}
        centres: list[tuple[int, int, int]] = []
        while active:
            seed = active.pop()
            stack = [seed]
            component = [seed]
            while stack:
                y, x = stack.pop()
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if not dx and not dy:
                            continue
                        neighbour = (y + dy, x + dx)
                        if neighbour in active:
                            active.remove(neighbour)
                            stack.append(neighbour)
                            component.append(neighbour)
            centres.append(
                (
                    round(sum(point[1] for point in component) / len(component)),
                    round(sum(point[0] for point in component) / len(component)),
                    len(component),
                )
            )
        return centres

    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas, "RGBA")
    # Source order is current, 10–20 and 20–30 minutes. Draw oldest first so a
    # new flash wins where intervals overlap. The dark outer halo and white ring
    # remain visible over both high reflectivity and bright cloud RGBs.
    styles = [
        # New flashes are illuminated white; older flashes fade through
        # yellow, never orange/coral where they could be confused with fire.
        ((255, 255, 255, 255), (255, 239, 116, 255), 6),
        ((255, 226, 76, 220), (255, 255, 255, 215), 5),
        ((207, 188, 82, 150), (255, 255, 255, 145), 4),
    ]
    for mask, (fill, ring, base_radius) in reversed(list(zip(masks, styles))):
        if mask is None:
            continue
        for x, y, area in component_centres(mask):
            radius = base_radius + min(2, max(0, area.bit_length() - 2))
            draw.ellipse((x - radius - 2, y - radius - 2, x + radius + 2, y + radius + 2), fill=(2, 7, 11, 225))
            draw.ellipse((x - radius - 1, y - radius - 1, x + radius + 1, y + radius + 1), fill=ring)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)
            if radius >= 5:
                bolt = [
                    (x + 1, y - radius + 2),
                    (x - 2, y),
                    (x, y),
                    (x - 1, y + radius - 2),
                    (x + 3, y - 1),
                    (x + 1, y - 1),
                ]
                draw.polygon(bolt, fill=(7, 13, 21, min(235, fill[3])))
    canvas.quantize(colors=32, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE).save(
        destination,
        "PNG",
        optimize=True,
    )


def render_watershed_overlay(
    domain: Domain,
    destination: Path,
    source_path: Path | None = None,
) -> None:
    """Render the local BC Hydro watershed polygons onto the aligned map grid."""
    from cartopy.io import shapereader
    from pyproj import CRS, Transformer
    from shapely.geometry import Polygon, box
    from shapely.ops import transform

    source = source_path or bch_watershed_source()
    projection = source.with_suffix(".prj")
    if not source.is_file():
        raise FileNotFoundError(f"BC Hydro watershed shapefile is missing: {source}")
    if not projection.is_file():
        raise FileNotFoundError(f"BC Hydro watershed projection is missing: {projection}")

    bbox = projected_bbox(domain)
    clip = box(*bbox)
    transformer = Transformer.from_crs(
        CRS.from_wkt(projection.read_text()),
        domain.crs,
        always_xy=True,
    )

    def rings(geometry: object):
        if isinstance(geometry, Polygon):
            yield geometry.exterior.coords
            for interior in geometry.interiors:
                yield interior.coords
            return
        for member in getattr(geometry, "geoms", ()):
            yield from rings(member)

    reader = shapereader.Reader(source)
    try:
        geometries = list(reader.geometries())
    finally:
        reader.close()

    xmin, ymin, xmax, ymax = bbox
    pixel_lines: list[list[tuple[float, float]]] = []
    for geometry in geometries:
        projected = transform(transformer.transform, geometry)
        if projected.is_empty or not projected.intersects(clip):
            continue
        clipped = projected.intersection(clip).simplify(150, preserve_topology=True)
        for coordinates in rings(clipped):
            pixels = [
                (
                    (x - xmin) / (xmax - xmin) * (domain.width - 1),
                    (ymax - y) / (ymax - ymin) * (domain.height - 1),
                )
                for x, y in coordinates
            ]
            if len(pixels) >= 2:
                pixel_lines.append(pixels)
    if not pixel_lines:
        raise RuntimeError("BC Hydro watershed shapefile does not intersect the map domain")

    image = Image.new("RGBA", (domain.width, domain.height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    for line in pixel_lines:
        draw.line(line, fill=(3, 16, 23, 215), width=3, joint="curve")
    for line in pixel_lines:
        draw.line(line, fill=(114, 217, 255, 225), width=1, joint="curve")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        image.save(temporary, "PNG", optimize=True)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def render_static_maps(domain: Domain, base_destination: Path, boundary_destination: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.patheffects as path_effects
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    projection = ccrs.epsg(int(domain.crs.split(":", 1)[1]))
    bbox = projected_bbox(domain)
    dpi = 120
    figsize = (domain.width / dpi, domain.height / dpi)

    def configure_axes(ax: object) -> None:
        ax.set_xlim(bbox[0], bbox[2])
        ax.set_ylim(bbox[1], bbox[3])
        ax.set_aspect("auto")
        ax.set_axis_off()

    base_destination.parent.mkdir(parents=True, exist_ok=True)
    figure = plt.figure(figsize=figsize, dpi=dpi, facecolor="#071018")
    axis = figure.add_axes([0, 0, 1, 1], projection=projection)
    configure_axes(axis)
    axis.add_feature(cfeature.OCEAN.with_scale("50m"), facecolor="#071018", zorder=0)
    axis.add_feature(cfeature.LAND.with_scale("50m"), facecolor="#18242c", zorder=1)
    axis.add_feature(cfeature.LAKES.with_scale("50m"), facecolor="#0b1720", edgecolor="#52616c", linewidth=0.35, zorder=2)
    axis.add_feature(cfeature.RIVERS.with_scale("50m"), edgecolor="#425664", linewidth=0.25, alpha=0.75, zorder=2)
    grid = axis.gridlines(
        crs=ccrs.PlateCarree(),
        draw_labels=False,
        linewidth=0.35,
        color="#60717c",
        alpha=0.28,
        linestyle=":",
        xlocs=range(-180, 181, 10 if domain.tier == "broad" else 5),
        ylocs=range(0 if domain.tier == "broad" else 40, 81, 10 if domain.tier == "broad" else 5),
    )
    figure.savefig(base_destination, dpi=dpi, transparent=False, pad_inches=0)
    plt.close(figure)

    figure = plt.figure(figsize=figsize, dpi=dpi, facecolor="none")
    axis = figure.add_axes([0, 0, 1, 1], projection=projection)
    configure_axes(axis)
    provinces = cfeature.NaturalEarthFeature(
        "cultural",
        "admin_1_states_provinces_lines",
        "10m",
        facecolor="none",
    )
    borders = cfeature.BORDERS.with_scale("10m")
    coastline = cfeature.COASTLINE.with_scale("10m")
    for feature, width in ((coastline, 2.8), (borders, 2.6), (provinces, 2.4)):
        axis.add_feature(feature, edgecolor="#071018", linewidth=width, alpha=0.86, zorder=5)
    for feature, width, alpha in ((coastline, 1.15, 0.96), (borders, 1.05, 0.94), (provinces, 0.72, 0.78)):
        axis.add_feature(feature, edgecolor="#f4f7f8", linewidth=width, alpha=alpha, zorder=6)

    cities = [
        ("Victoria", -123.37, 48.43),
        ("Vancouver", -123.12, 49.28),
        ("Kelowna", -119.49, 49.89),
        ("Kamloops", -120.33, 50.67),
        ("Prince George", -122.75, 53.92),
        ("Williams Lake", -122.14, 52.13),
        ("Terrace", -128.60, 54.52),
        ("Prince Rupert", -130.32, 54.32),
        ("Fort St. John", -120.85, 56.25),
        ("Cranbrook", -115.77, 49.51),
    ]
    for name, lon, lat in cities if domain.id == "bc" else []:
        axis.plot(lon, lat, marker="o", markersize=2.8, color="#ffffff", markeredgecolor="#071018", markeredgewidth=0.9, transform=ccrs.PlateCarree(), zorder=8)
        text = axis.text(
            lon + 0.18,
            lat + 0.10,
            name,
            transform=ccrs.PlateCarree(),
            color="#ffffff",
            fontsize=7.3,
            weight="medium",
            zorder=9,
        )
        text.set_path_effects([path_effects.Stroke(linewidth=2.1, foreground="#071018"), path_effects.Normal()])
    figure.savefig(boundary_destination, dpi=dpi, transparent=True, pad_inches=0)
    plt.close(figure)
