from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from .config import Domain
from .geomet import projected_bbox


def save_satellite(content: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = Image.open(io.BytesIO(content)).convert("RGB")
    image.save(destination, "WEBP", quality=88, method=6)


def save_overlay(content: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = Image.open(io.BytesIO(content)).convert("RGBA")
    image = image.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    image.save(destination, "PNG", optimize=True)


def save_coverage(content: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = Image.open(io.BytesIO(content)).convert("RGBA")
    array = np.asarray(image).copy()
    # GeoMet's ``*.INV``/``*-Inverted`` coverage layers paint the area where
    # a radar estimate is available.  The viewer needs the opposite semantic:
    # hatch only the area with *no* current coverage.  Inverting here also
    # prevents valid precipitation echoes from being obscured by the hatch.
    mask = array[:, :, 3] <= 20
    y, x = np.indices(mask.shape)
    hatch = ((x + y) % 10) < 1
    array[:, :, :3] = np.where(mask[:, :, None], np.array([151, 160, 170], dtype=np.uint8), 0)
    array[:, :, 3] = np.where(mask, np.where(hatch, 78, 30), 0).astype(np.uint8)
    output = Image.fromarray(array, "RGBA").quantize(
        colors=16,
        method=Image.Quantize.FASTOCTREE,
        dither=Image.Dither.NONE,
    )
    output.save(destination, "PNG", optimize=True)


def lightning_trail(source_paths: list[Path | None], destination: Path) -> None:
    """Render current, 10–20, and 20–30 minute lightning in age colours."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    size: tuple[int, int] | None = None
    masks: list[Image.Image | None] = []
    for path in source_paths:
        if path is None or not path.exists():
            masks.append(None)
            continue
        source = Image.open(path).convert("RGBA")
        size = source.size
        alpha = source.getchannel("A").point(lambda value: 255 if value > 20 else 0)
        masks.append(alpha)
    if size is None:
        raise ValueError("At least one lightning source frame is required")
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    # Draw oldest first. A one-pixel source cell becomes a compact, haloed glyph.
    colours = [(255, 159, 28, 255), (0, 229, 255, 255), (255, 255, 255, 255)]
    for mask, colour in zip(reversed(masks), colours):
        if mask is None:
            continue
        halo_mask = mask.filter(ImageFilter.MaxFilter(9))
        core_mask = mask.filter(ImageFilter.MaxFilter(5))
        halo = Image.new("RGBA", size, (5, 8, 12, 235))
        core = Image.new("RGBA", size, colour)
        canvas.alpha_composite(Image.composite(halo, Image.new("RGBA", size), halo_mask))
        canvas.alpha_composite(Image.composite(core, Image.new("RGBA", size), core_mask))
    canvas.quantize(colors=32, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE).save(
        destination,
        "PNG",
        optimize=True,
    )


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
        xlocs=range(-180, -79, 5),
        ylocs=range(40, 76, 5),
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
    for name, lon, lat in cities:
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
