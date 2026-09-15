"""Publish one enhanced MSC raster while retaining private source pixels for video."""
from __future__ import annotations
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import tempfile
from PIL import Image
from . import cloud_style

ENHANCED_LAYER = 'eccc-geocolor-enhanced'

def ensure_enhanced_msc(root: Path, metadata: dict) -> Path:
    source = root / metadata['path']
    relative = metadata['path'].replace('/eccc-geocolor/', f'/{ENHANCED_LAYER}/')
    if relative == metadata['path']:
        raise ValueError('Expected an MSC source raster')
    destination = root / relative
    meta_path = root / relative.replace('frames/', 'metadata/', 1)
    meta_path = meta_path.with_suffix('.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    lock_root = root / 'composite-frame-cache' / 'cloud-light'
    lock_root.mkdir(parents=True, exist_ok=True)
    # One producer per observation; the lock file stays inside the bounded cache tree.
    import hashlib
    lock = lock_root / ('.raster-lock-' + hashlib.sha256(relative.encode()).hexdigest()[:2])
    with lock.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        stat = source.stat()
        identity = [stat.st_size, stat.st_mtime_ns, metadata.get('fetchedAt'), cloud_style.STYLE_VERSION]
        try:
            previous = json.loads(meta_path.read_text())
            if previous.get('enhancementInput') == identity and destination.is_file():
                return destination
        except (OSError, ValueError):
            pass
        with Image.open(source) as image:
            source_time = dt.datetime.fromisoformat(metadata['validTime'].replace('Z', '+00:00'))
            enhanced = cloud_style.render(image, source_time, geography=cloud_style.geography(
                'bc', {'left': 0, 'top': 0, 'width': 1, 'height': 1}))
            if 'A' in image.getbands():
                enhanced.putalpha(image.getchannel('A'))
        temporary = destination.with_name(f'.{destination.name}.{os.getpid()}.tmp')
        try:
            enhanced.save(temporary, 'WEBP', quality=88, method=4)
            temporary.replace(destination)
        finally:
            enhanced.close()
            temporary.unlink(missing_ok=True)
        payload = {**metadata, 'path': relative, 'satelliteStyle': cloud_style.STYLE_VERSION,
                   'enhancementInput': identity}
        with tempfile.NamedTemporaryFile(mode='w', dir=meta_path.parent, delete=False) as out:
            json.dump(payload, out)
            temporary_meta = Path(out.name)
        temporary_meta.replace(meta_path)
        return destination


def expose_enhanced_msc(catalog: dict) -> None:
    """Public MSC is always enhanced; source rasters remain local encoder inputs."""
    layers = catalog.get('domains', {}).get('bc', {}).get('layers', {})
    original = layers.get('eccc-geocolor')
    enhanced = layers.pop(ENHANCED_LAYER, None)
    if original is not None:
        layers['eccc-geocolor'] = {**original, 'title': 'Enhanced MSC GeoColour',
                                  'frames': [f for f in (enhanced or original).get('frames', [])
                                             if f.get('satelliteStyle') == cloud_style.STYLE_VERSION
                                             and f'/{ENHANCED_LAYER}/' in f.get('path', '')]}
