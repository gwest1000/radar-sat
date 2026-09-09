"""BC XL short-loop pilot: source-preserving layered daylight, soft colour, 50%.

No model inference. Fail the whole generation on an enhancement error; never
substitute a differently graded frame. Cache files live in the existing bounded,
unpublished composite-frame cache and are shared by ranges and overlay presets.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import numpy as np
from PIL import Image, ImageFilter

ASSETS = Path(__file__).with_name("cloud_light")
STYLE_NAME = "layered-daylight-soft-50-v1"
_STYLE_DIGEST = hashlib.sha256(Path(__file__).read_bytes() + b"".join(
    p.read_bytes() for p in sorted(ASSETS.iterdir()) if p.suffix in {".mjs", ".json"}
)).hexdigest()[:16]
STYLE_VERSION = f"{STYLE_NAME}-{_STYLE_DIGEST}"
LEGACY_STYLE = "saturate(0.52) brightness(0.78) contrast(1.06)"


def pilot_enabled(spec, hours):
    return (spec.product_id == "bc-large-overlay" and spec.layer_id == "eccc-geocolor"
            and spec.track == "live" and hours in (3, 6))


def vivid(image):
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    luma = rgb @ np.array([.2126, .7152, .0722], dtype=np.float32)
    gray = Image.fromarray(np.rint(luma * 255).astype(np.uint8))
    scale = image.width / 1920
    broad = np.asarray(gray.filter(ImageFilter.GaussianBlur(10 * scale)), dtype=np.float32) / 255
    fine = np.asarray(gray.filter(ImageFilter.GaussianBlur(.85 * scale)), dtype=np.float32) / 255
    detail = np.clip(.42 * (luma - broad) + .38 * (luma - fine), -.045, .045)
    tone = np.clip(luma + .16 * (luma - .5) * (1 - (2 * luma - 1) ** 2) + detail, 0, 1)
    saturation = 1 + .20 * (4 * luma * (1 - luma))
    enhanced = luma[..., None] + saturation[..., None] * (rgb - luma[..., None]) + (tone-luma)[..., None]
    return Image.fromarray(np.rint(np.clip(enhanced, 0, 1)*255).astype(np.uint8))


def render(image, source_time):
    node = os.environ.get("RADARSAT_CLOUD_NODE") or shutil.which("node")
    if not node and Path("/opt/homebrew/bin/node").is_file():
        node = "/opt/homebrew/bin/node"  # launchd has a minimal PATH on this host.
    if not node:
        raise RuntimeError("BC XL cloud pilot requires Node.js; retaining complete prior loop")
    baseline = vivid(image)
    try:
        payload = image.convert("RGBA").tobytes() + baseline.convert("RGBA").tobytes()
        result = subprocess.run([node, str(ASSETS / "worker.mjs"), str(image.width),
                                 str(image.height), source_time.isoformat()],
                                input=payload, capture_output=True, timeout=30, check=True)
    finally:
        baseline.close()
    if len(result.stdout) != image.width * image.height * 4:
        raise RuntimeError("Incomplete cloud enhancement output")
    return Image.frombytes("RGBA", image.size, result.stdout).convert("RGB")


def cache_path(output_root, source_root, spec, frame):
    def identity(relative):
        p = source_root / relative
        stat = p.stat()
        return [relative, stat.st_size, stat.st_mtime_ns]
    key = {"style": STYLE_VERSION, "source": identity(frame.source_path),
           "fetchedAt": frame.source_fetched_at, "sourceTime": frame.source_valid_time.isoformat(),
           "base": identity(f"static/{spec.domain_id}/base-dark.png"),
           "viewport": dict(spec.viewport), "size": [spec.width, spec.height]}
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:32]
    return output_root / "composite-frame-cache" / "cloud-light" / f"{digest}.png"


def read_cache(path, size):
    try:
        with Image.open(path) as image:
            if image.size != size or image.mode != "RGB":
                return None
            result = image.copy()  # Decode now: truncated cache entries are rebuilt.
        path.touch()
        return result
    except (OSError, ValueError):
        return None


def render_cached(image, source_time, path, write_image):
    # Fixed 256 lock buckets bound disk metadata; flock is released even if a
    # worker is killed. Recheck after waiting so concurrent 3h/6h jobs share work.
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / f".lock-{path.stem[:2]}").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cached = read_cache(path, image.size)
        if cached is not None:
            return cached
        result = render(image, source_time)
        try:
            write_image(path, result)
        except BaseException:
            result.close()
            raise
        return result
