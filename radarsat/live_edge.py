from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import DOMAINS, LAYERS
from .r2 import LIVE_EDGE_KEY, LocalObject, R2Config, boto3_client, upload_catalog, upload_object


UTC = dt.timezone.utc
LIVE_EDGE_BASE_LAYERS = frozenset(
    {
        "eccc-geocolor",
        "radar-rain",
        "radar-coverage",
        "lightning-trail",
        "glm-lightning-trail",
        "glm-lightning-live",
    }
)


def _metadata_payload(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not all(isinstance(payload.get(key), str) for key in ("validTime", "path", "fetchedAt")):
        return None
    return payload


def _latest_metadata(root: Path, domain_id: str, layer_id: str) -> dict[str, Any] | None:
    directory = root / "metadata" / domain_id / layer_id
    if not directory.is_dir():
        return None
    for path in sorted(directory.rglob("*.json"), reverse=True):
        payload = _metadata_payload(path)
        if payload is None:
            continue
        relative = Path(str(payload["path"]))
        if relative.is_absolute() or ".." in relative.parts or not (root / relative).is_file():
            continue
        return payload
    return None


def live_edge_layer_ids() -> tuple[str, ...]:
    return tuple(sorted(
        layer_id
        for layer_id in LAYERS
        if layer_id in LIVE_EDGE_BASE_LAYERS
        or any(layer_id.startswith(f"{base}-region-") for base in (
            "radar-rain",
            "lightning-trail",
            "glm-lightning-live",
        ))
    ))


def build_live_edge_index(
    root: Path,
    *,
    now: dt.datetime | None = None,
) -> tuple[dict[str, Any], list[LocalObject]]:
    root = root.resolve()
    generated = (now or dt.datetime.now(UTC)).astimezone(UTC)
    domains: dict[str, Any] = {}
    objects: dict[str, LocalObject] = {}
    for domain_id in DOMAINS:
        layers: dict[str, Any] = {}
        for layer_id in live_edge_layer_ids():
            from .cloud_raster import ENHANCED_LAYER
            source_layer_id = ENHANCED_LAYER if domain_id == "bc" and layer_id == "eccc-geocolor" else layer_id
            frame = _latest_metadata(root, domain_id, source_layer_id)
            if frame is None:
                continue
            layer = LAYERS[layer_id]
            layers[layer_id] = {
                "title": layer.title,
                "maxAgeMinutes": layer.max_age_minutes,
                "frames": [frame],
            }
            relative = Path(str(frame["path"]))
            path = root / relative
            stat = path.stat()
            objects[relative.as_posix()] = LocalObject(
                key=relative.as_posix(),
                path=path,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        if layers:
            domains[domain_id] = {"layers": layers}
    return ({
        "schemaVersion": 1,
        "generatedAt": generated.isoformat().replace("+00:00", "Z"),
        "domains": domains,
    }, sorted(objects.values(), key=lambda item: item.key))


def publish_live_edge(
    root: Path,
    config: R2Config,
    *,
    client: Any | None = None,
    now: dt.datetime | None = None,
    state_path: Path | None = None,
) -> dict[str, object]:
    payload, objects = build_live_edge_index(root, now=now)
    if not objects:
        raise RuntimeError("No live-edge satellite, radar, or lightning objects are available")
    r2_client = client or boto3_client(config)
    from .publication_content import enabled, project_frames, atomic_bytes, semantic_digest
    from .publication_ledger import put_if_changed, put_pointer, object_lock
    from .r2 import PublishState
    if enabled():
        keys = project_frames(root, payload, bundles=False)
        objects = [LocalObject(key, root / key, (root / key).stat().st_size,
                               (root / key).stat().st_mtime_ns) for key in sorted(keys)]
    shared_state = (state_path.parent if state_path is not None else root / '.publish-state') / 'r2-publish.sqlite3'
    state = PublishState(shared_state, f"{config.account_id}/{config.bucket}")
    with object_lock(shared_state, "publication-gc"):
        state.protect_live_edge((item.key for item in objects), dt.datetime.now(dt.timezone.utc))
        known = state.known_objects()
    state.close()
    changed = [item for item in objects if known.get(item.key) != (item.size, item.mtime_ns)]
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(changed)))) as executor:
        results = list(executor.map(lambda item: put_if_changed(r2_client, config, item, shared_state), changed))
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    index_uploaded = put_pointer(r2_client, config, encoded, LIVE_EDGE_KEY, shared_state)
    # Never let a slower rapid publisher roll the local recovery pointer back.
    with object_lock(shared_state, 'local-live-edge'):
        local = root / LIVE_EDGE_KEY
        previous = local.read_bytes() if local.exists() else None
        if previous is None or (payload['generatedAt'] >= json.loads(previous).get('generatedAt', '')
                                and semantic_digest(previous) != semantic_digest(encoded)):
            atomic_bytes(local, encoded)
    return {
        "status": "published",
        "generatedAt": payload["generatedAt"],
        "objects": len(objects),
        "uploadedObjects": sum(int(result[1]) for result in results),
        "indexUploaded": index_uploaded,
        "contentReused": sum(int(not result[1]) for result in results),
        "bytes": sum(item.size for item in objects),
        "hashes": len(results),
        "url": f"{config.public_base_url}/{LIVE_EDGE_KEY}" if config.public_base_url else LIVE_EDGE_KEY,
    }
