"""Operational eligibility, separate from the unchanged appearance/cache version."""
from dataclasses import replace
from PIL import Image
from . import cloud_style


def enabled(spec, hours=None):
    return spec.product_id.startswith("bc-") and spec.layer_id == "eccc-geocolor"


def frame_image(source_root, output_root, spec, frame):
    # Grade each map crop once at its canonical resolution for every track/rendition.
    # Reuse the accepted sidecar cache, then downsample efficient renditions.
    from .video import _crop_resize, _display_size
    from .composite_video import _atomic_png
    width, height = _display_size(spec.domain_id, spec.viewport)
    canonical = replace(spec, width=width, height=height)
    path = cloud_style.cache_path(output_root, source_root, canonical, frame)
    image = cloud_style.read_cache(path, (width, height))
    if image is None:
        with Image.open(source_root / f"static/{spec.domain_id}/base-dark.png") as source:
            base = _crop_resize(source, spec.viewport, width, height).convert("RGB")
        with Image.open(source_root / frame.source_path) as source:
            satellite = _crop_resize(source, spec.viewport, width, height)
            base.paste(satellite.convert("RGB"), (0, 0), satellite.getchannel("A"))
            satellite.close()
        try:
            image = cloud_style.render_cached(
                base, frame.source_valid_time, path, _atomic_png,
                geography=cloud_style.geography(spec.domain_id, spec.viewport),
            )
        finally:
            base.close()
    if image.size != (spec.width, spec.height):
        resized = image.resize((spec.width, spec.height), Image.Resampling.LANCZOS)
        image.close()
        image = resized
    return image
