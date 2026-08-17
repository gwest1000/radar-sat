from __future__ import annotations

import io
import json
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .config import Domain
from .geomet import projected_bbox, projection_longitude


DEFAULT_BCH_WATERSHEDS = (
    Path(__file__).resolve().parents[2]
    / "fcstGraphics"
    / "data"
    / "bc_watersheds"
    / "bch"
    / "AllWatershedsUTM.shp"
)
DEFAULT_TRANSMISSION_LINES = (
    Path(__file__).resolve().parents[2]
    / "fcstGraphics"
    / "data"
    / "bc_transmission_lines.geojson"
)


def bch_watershed_source() -> Path:
    configured = os.environ.get("RADARSAT_BCH_WATERSHEDS")
    return Path(configured).expanduser() if configured else DEFAULT_BCH_WATERSHEDS


def transmission_line_source() -> Path:
    configured = os.environ.get("RADARSAT_TRANSMISSION_LINES")
    return Path(configured).expanduser() if configured else DEFAULT_TRANSMISSION_LINES


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
    # GeoMet's coverage render paints the circular radar footprints. Hatch its
    # non-transparent pixels and leave the area outside radar coverage clear.
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


def lightning_trail(
    source_paths: list[Path | None],
    destination: Path,
    *,
    scale: int = 1,
    viewport: dict[str, float] | None = None,
    output_width: int | None = None,
    symbol_reference_width: int = 960,
    blur_glow: bool = True,
    arrival_only: bool = False,
    new_strike_halo: bool = True,
) -> None:
    """Render age-fading lightning clusters or a dedicated arrival flash."""
    if scale < 1:
        raise ValueError("Lightning raster scale must be at least one")
    if symbol_reference_width < 1:
        raise ValueError("Lightning symbol reference width must be positive")
    if viewport is not None and output_width is None:
        raise ValueError("Regional lightning renders require an output width")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_size: tuple[int, int] | None = None
    masks: list[np.ndarray | None] = []
    for path in source_paths:
        if path is None or not path.exists():
            masks.append(None)
            continue
        source = Image.open(path).convert("RGBA")
        current_source_size = source.size
        if source_size is not None and current_source_size != source_size:
            raise ValueError("Lightning source frames do not share a common grid")
        source_size = current_source_size
        mask = np.asarray(source.getchannel("A")) > 20
        masks.append(mask)
    if source_size is None:
        raise ValueError("At least one lightning source frame is required")
    if viewport is not None and output_width is not None:
        crop_width = source_size[0] * viewport["width"]
        crop_height = source_size[1] * viewport["height"]
        size = (output_width, max(1, round(output_width * crop_height / crop_width)))
        symbol_scale = output_width / symbol_reference_width
    elif output_width is not None:
        size = (
            output_width,
            max(1, round(output_width * source_size[1] / source_size[0])),
        )
        symbol_scale = output_width / symbol_reference_width
    else:
        size = (source_size[0] * scale, source_size[1] * scale)
        symbol_scale = float(scale)

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

    def output_point(source_x: int, source_y: int) -> tuple[int, int] | None:
        if viewport is None:
            if output_width is not None:
                return (
                    round(source_x / max(1, source_size[0] - 1) * (size[0] - 1)),
                    round(source_y / max(1, source_size[1] - 1) * (size[1] - 1)),
                )
            return (
                round(source_x * scale + (scale - 1) / 2),
                round(source_y * scale + (scale - 1) / 2),
            )
        normalized_x = source_x / max(1, source_size[0] - 1)
        normalized_y = source_y / max(1, source_size[1] - 1)
        relative_x = (normalized_x - viewport["left"]) / viewport["width"]
        relative_y = (normalized_y - viewport["top"]) / viewport["height"]
        margin = 0.025
        if not (-margin <= relative_x <= 1 + margin and -margin <= relative_y <= 1 + margin):
            return None
        return (
            round(relative_x * (size[0] - 1)),
            round(relative_y * (size[1] - 1)),
        )

    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    # Source order is current followed by successive ten-minute bins. Draw the
    # oldest first so a new flash wins where intervals overlap. These are the
    # same illuminated white-to-yellow bolt symbols used before lightning moved
    # to a lightweight transparent raster; the dark outline remains visible
    # over bright cloud.
    styles = [
        ((255, 254, 240, 255), (255, 255, 255, 180), round(6 * symbol_scale)),
        ((255, 242, 154, 210), (255, 255, 255, 120), round(5.25 * symbol_scale)),
        ((255, 224, 100, 145), (255, 255, 255, 70), round(4.5 * symbol_scale)),
        ((246, 212, 81, 112), (255, 250, 190, 42), round(4.1 * symbol_scale)),
        ((232, 195, 70, 82), (255, 246, 180, 28), round(3.8 * symbol_scale)),
        ((215, 178, 62, 58), (255, 240, 165, 18), round(3.5 * symbol_scale)),
    ]
    rendered_new_strike_glow = False
    for age_index, (mask, (fill, glow, half_height)) in reversed(list(enumerate(zip(masks, styles)))):
        if mask is None or (arrival_only and age_index != 0):
            continue
        bolt_layer = Image.new("RGBA", size, (0, 0, 0, 0))
        bolt_draw = ImageDraw.Draw(bolt_layer, "RGBA")
        new_strike_glow = (
            Image.new("RGBA", size, (0, 0, 0, 0))
            if age_index == 0 and new_strike_halo
            else None
        )
        for source_x, source_y, area in component_centres(mask):
            point = output_point(source_x, source_y)
            if point is None:
                continue
            x, y = point
            height = half_height + round(
                min(2 * symbol_scale, max(0, area.bit_length() - 2) * symbol_scale)
            )
            width = max(round(5 * symbol_scale), round(height * 0.72))
            bolt = [
                (x + round(width * 0.18), y - height),
                (x - width, y + round(height * 0.08)),
                (x - round(width * 0.15), y + round(height * 0.08)),
                (x - round(width * 0.38), y + height),
                (x + width, y - round(height * 0.18)),
                (x + round(width * 0.18), y - round(height * 0.18)),
            ]
            closed_bolt = bolt + [bolt[0]]
            if new_strike_glow is not None:
                # Blur only a small patch around each new strike. This keeps
                # the raster cheap to generate while producing a genuinely
                # diffuse halo rather than visible concentric rings or a
                # translucent white disk.
                radius = max(9, round(12 * symbol_scale))
                padding = radius * 3
                patch_size = padding * 2 + 1
                patch_alpha = Image.new("L", (patch_size, patch_size), 0)
                patch_draw = ImageDraw.Draw(patch_alpha)
                centre = padding
                core = max(2, round(4.5 * symbol_scale))
                patch_draw.ellipse(
                    (
                        centre - core,
                        centre - core,
                        centre + core,
                        centre + core,
                    ),
                    fill=255,
                )
                patch_alpha = patch_alpha.filter(
                    ImageFilter.GaussianBlur(radius=max(2.8, radius * 0.30))
                )
                patch = Image.new(
                    "RGBA",
                    (patch_size, patch_size),
                    (255, 254, 235, 0),
                )
                patch.putalpha(patch_alpha)
                destination_left = x - padding
                destination_top = y - padding
                destination_right = destination_left + patch_size
                destination_bottom = destination_top + patch_size
                clipped_left = max(0, destination_left)
                clipped_top = max(0, destination_top)
                clipped_right = min(size[0], destination_right)
                clipped_bottom = min(size[1], destination_bottom)
                if clipped_left < clipped_right and clipped_top < clipped_bottom:
                    source_box = (
                        clipped_left - destination_left,
                        clipped_top - destination_top,
                        clipped_right - destination_left,
                        clipped_bottom - destination_top,
                    )
                    new_strike_glow.alpha_composite(
                        patch.crop(source_box),
                        (clipped_left, clipped_top),
                    )
                    rendered_new_strike_glow = True
                if arrival_only:
                    continue
            bolt_draw.line(
                closed_bolt,
                fill=(2, 7, 11, min(225, fill[3])),
                width=max(3, round(5 * symbol_scale)),
                joint="curve",
            )
            bolt_draw.line(
                closed_bolt,
                fill=glow,
                width=max(2, round(3 * symbol_scale)),
                joint="curve",
            )
            bolt_draw.polygon(bolt, fill=fill)
        if new_strike_glow is not None:
            canvas.alpha_composite(new_strike_glow)
        canvas.alpha_composite(bolt_layer)
    if arrival_only or rendered_new_strike_glow:
        # Preserve straight-alpha white RGB values in the diffuse age-zero
        # halo. FASTOCTREE premultiplies translucent pixels and turns it into
        # a dark disk when composited over satellite imagery.
        canvas.save(destination, "PNG", optimize=True)
    else:
        canvas.quantize(
            colors=32,
            method=Image.Quantize.FASTOCTREE,
            dither=Image.Dither.NONE,
        ).save(destination, "PNG", optimize=True)


def render_watershed_overlay(
    domain: Domain,
    destination: Path,
    source_path: Path | None = None,
    *,
    viewport: dict[str, float] | None = None,
    output_width: int | None = None,
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

    if viewport is not None and output_width is None:
        raise ValueError("Regional watershed renders require an output width")
    if output_width is not None and output_width < 1:
        raise ValueError("Watershed overlay output width must be positive")

    full_bbox = projected_bbox(domain)
    if viewport is None:
        bbox = full_bbox
        render_size = (
            output_width or domain.width,
            max(
                1,
                round((output_width or domain.width) * domain.height / domain.width),
            ),
        )
    else:
        full_xmin, full_ymin, full_xmax, full_ymax = full_bbox
        full_width = full_xmax - full_xmin
        full_height = full_ymax - full_ymin
        xmin = full_xmin + viewport["left"] * full_width
        xmax = xmin + viewport["width"] * full_width
        ymax = full_ymax - viewport["top"] * full_height
        ymin = ymax - viewport["height"] * full_height
        bbox = (xmin, ymin, xmax, ymax)
        assert output_width is not None
        render_size = (
            output_width,
            max(
                1,
                round(
                    output_width
                    * domain.height
                    * viewport["height"]
                    / (domain.width * viewport["width"])
                ),
            ),
        )
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
        clipped = projected.intersection(clip).simplify(
            30 if viewport is not None else 150,
            preserve_topology=True,
        )
        for coordinates in rings(clipped):
            pixels = [
                (
                    (x - xmin) / (xmax - xmin) * (render_size[0] - 1),
                    (ymax - y) / (ymax - ymin) * (render_size[1] - 1),
                )
                for x, y in coordinates
            ]
            if len(pixels) >= 2:
                pixel_lines.append(pixels)
    if not pixel_lines:
        raise RuntimeError("BC Hydro watershed shapefile does not intersect the map domain")

    image = Image.new("RGBA", render_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    core_width = max(1, round(render_size[0] / 1440))
    halo_width = core_width + max(2, round(render_size[0] / 1440))
    for line in pixel_lines:
        draw.line(line, fill=(3, 16, 23, 215), width=halo_width, joint="curve")
    for line in pixel_lines:
        draw.line(line, fill=(114, 217, 255, 225), width=core_width, joint="curve")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        image.save(temporary, "PNG", optimize=True)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def render_transmission_overlay(
    domain: Domain,
    destination: Path,
    source_path: Path | None = None,
    *,
    output_width: int | None = None,
) -> None:
    """Render the GeoBC transmission network onto the aligned map grid."""
    from pyproj import Transformer

    source = source_path or transmission_line_source()
    if not source.is_file():
        raise FileNotFoundError(f"BC transmission-line GeoJSON is missing: {source}")
    payload = json.loads(source.read_text())
    features = payload.get("features", [])
    if payload.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise RuntimeError("BC transmission-line source is not a GeoJSON FeatureCollection")

    def coordinate_lines(geometry: object):
        if not isinstance(geometry, dict):
            return
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates")
        if geometry_type == "LineString" and isinstance(coordinates, list):
            yield coordinates
        elif geometry_type == "MultiLineString" and isinstance(coordinates, list):
            yield from coordinates

    transformer = Transformer.from_crs(
        "EPSG:4326",
        domain.crs,
        always_xy=True,
        force_over=True,
    )
    xmin, ymin, xmax, ymax = projected_bbox(domain)
    pixel_lines: list[list[tuple[float, float]]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        for coordinates in coordinate_lines(feature.get("geometry")):
            values: list[tuple[float, float]] = []
            for coordinate in coordinates:
                if not isinstance(coordinate, list) or len(coordinate) < 2:
                    continue
                try:
                    longitude = projection_longitude(float(coordinate[0]), domain)
                    latitude = float(coordinate[1])
                except (TypeError, ValueError):
                    continue
                x, y = transformer.transform(longitude, latitude)
                if not (np.isfinite(x) and np.isfinite(y)):
                    continue
                values.append((x, y))
            if len(values) < 2:
                continue
            line_x = [point[0] for point in values]
            line_y = [point[1] for point in values]
            if max(line_x) < xmin or min(line_x) > xmax or max(line_y) < ymin or min(line_y) > ymax:
                continue
            pixel_lines.append([
                (
                    (x - xmin) / (xmax - xmin) * (domain.width - 1),
                    (ymax - y) / (ymax - ymin) * (domain.height - 1),
                )
                for x, y in values
            ])
    if not pixel_lines:
        raise RuntimeError("BC transmission-line source does not intersect the map domain")

    render_width = output_width or domain.width
    if render_width < 1:
        raise ValueError("Transmission overlay output width must be positive")
    render_scale = render_width / domain.width
    render_size = (
        render_width,
        max(1, round(render_width * domain.height / domain.width)),
    )
    scaled_lines = [
        [(x * render_scale, y * render_scale) for x, y in line]
        for line in pixel_lines
    ]
    image = Image.new("RGBA", render_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    core_width = max(2, round(render_width / 960))
    halo_width = core_width + max(2, round(render_width / 960))
    for line in scaled_lines:
        draw.line(line, fill=(2, 7, 11, 235), width=halo_width, joint="curve")
    for line in scaled_lines:
        draw.line(line, fill=(255, 255, 255, 242), width=core_width, joint="curve")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        image.quantize(
            colors=16,
            method=Image.Quantize.FASTOCTREE,
            dither=Image.Dither.NONE,
        ).save(temporary, "PNG", optimize=True)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def render_static_maps(
    domain: Domain,
    base_destination: Path,
    boundary_destination: Path,
    *,
    boundary_scale: int = 2,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.patheffects as path_effects
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    import cartopy.io.shapereader as shapereader
    from pyproj import Transformer
    from shapely.geometry import box
    from shapely.ops import transform as transform_geometry

    projection = ccrs.epsg(int(domain.crs.split(":", 1)[1]))
    bbox = projected_bbox(domain)
    dpi = 120
    figsize = (domain.width / dpi, domain.height / dpi)
    if boundary_scale < 1:
        raise ValueError("Boundary render scale must be at least one")

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
    temporary_base = base_destination.with_suffix(base_destination.suffix + ".tmp")
    figure.savefig(
        temporary_base,
        dpi=dpi,
        transparent=False,
        pad_inches=0,
        format="png",
    )
    temporary_base.replace(base_destination)
    plt.close(figure)

    boundary_figsize = (
        domain.width * boundary_scale / dpi,
        domain.height * boundary_scale / dpi,
    )
    figure = plt.figure(figsize=boundary_figsize, dpi=dpi, facecolor="none")
    if domain.id == "bc":
        # Cartopy clips EPSG:3005 features to the CRS's official BC area of
        # use, even though our operational grid intentionally extends well
        # into neighbouring provinces, territories and U.S. states. Project
        # Natural Earth linework directly onto an ordinary axis so boundaries
        # cover the complete raster instead of stopping at the BC outline.
        axis = figure.add_axes([0, 0, 1, 1])
        configure_axes(axis)
        clip_box = box(*bbox)
        transformer = Transformer.from_crs("EPSG:4326", domain.crs, always_xy=True)

        def geometry_segments(geometry: object) -> list[list[tuple[float, float]]]:
            geometry_type = getattr(geometry, "geom_type", "")
            if geometry_type in {"LineString", "LinearRing"}:
                coordinates = list(geometry.coords)
                return [coordinates] if len(coordinates) >= 2 else []
            segments: list[list[tuple[float, float]]] = []
            for child in getattr(geometry, "geoms", ()):
                segments.extend(geometry_segments(child))
            return segments

        def natural_earth_segments(category: str, name: str) -> list[list[tuple[float, float]]]:
            source = shapereader.natural_earth("10m", category, name)
            segments: list[list[tuple[float, float]]] = []
            for geometry in shapereader.Reader(source).geometries():
                west, south, east, north = geometry.bounds
                if east < -175 or west > -80 or north < 30 or south > 75:
                    continue
                try:
                    projected = transform_geometry(transformer.transform, geometry)
                    clipped = projected.intersection(clip_box)
                except Exception:
                    continue
                segments.extend(geometry_segments(clipped))
            return segments

        # Coastlines, international borders and state/province borders use
        # one deliberately subdued hierarchy.  A dark casing keeps every line
        # legible over bright cloud, while the narrower gray centre avoids the
        # stark white country/coast emphasis used by the earlier render.
        linework = tuple(
            (
                natural_earth_segments(category, name),
                2.4,
                0.72,
                0.78,
            )
            for category, name in (
                ("physical", "coastline"),
                ("cultural", "admin_0_boundary_lines_land"),
                ("cultural", "admin_1_states_provinces_lines"),
            )
        )
        for segments, dark_width, light_width, light_alpha in linework:
            axis.add_collection(LineCollection(
                segments,
                colors="#071018",
                linewidths=dark_width * boundary_scale,
                alpha=0.86,
                zorder=5,
            ))
            axis.add_collection(LineCollection(
                segments,
                colors="#f4f7f8",
                linewidths=light_width * boundary_scale,
                alpha=light_alpha,
                zorder=6,
            ))
    else:
        axis = figure.add_axes([0, 0, 1, 1], projection=projection)
        configure_axes(axis)
        provinces = cfeature.NaturalEarthFeature(
            "cultural",
            "admin_1_states_provinces",
            "10m",
            facecolor="none",
        )
        borders = cfeature.BORDERS.with_scale("10m")
        coastline = cfeature.COASTLINE.with_scale("10m")
        for feature in (coastline, borders, provinces):
            axis.add_feature(
                feature,
                edgecolor="#071018",
                linewidth=2.4 * boundary_scale,
                alpha=0.86,
                zorder=5,
            )
        for feature in (coastline, borders, provinces):
            axis.add_feature(
                feature,
                edgecolor="#f4f7f8",
                linewidth=0.72 * boundary_scale,
                alpha=0.78,
                zorder=6,
            )

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
        city_x, city_y = transformer.transform(lon, lat)
        axis.plot(
            city_x,
            city_y,
            marker="o",
            markersize=2.8 * boundary_scale,
            color="#ffffff",
            markeredgecolor="#071018",
            markeredgewidth=0.9 * boundary_scale,
            zorder=8,
        )
        text = axis.text(
            city_x + (bbox[2] - bbox[0]) * 0.0048,
            city_y + (bbox[3] - bbox[1]) * 0.0038,
            name,
            color="#ffffff",
            fontsize=7.3 * boundary_scale,
            weight="medium",
            zorder=9,
        )
        text.set_path_effects([
            path_effects.Stroke(
                linewidth=2.1 * boundary_scale,
                foreground="#071018",
            ),
            path_effects.Normal(),
        ])
    temporary_boundaries = boundary_destination.with_suffix(
        boundary_destination.suffix + ".tmp"
    )
    figure.savefig(
        temporary_boundaries,
        dpi=dpi,
        transparent=True,
        pad_inches=0,
        format="png",
    )
    temporary_boundaries.replace(boundary_destination)
    plt.close(figure)
