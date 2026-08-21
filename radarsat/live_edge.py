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
            frame = _latest_metadata(root, domain_id, layer_id)
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
    previous: dict[str, list[int]] = {}
    if state_path is not None:
        try:
            decoded = json.loads(state_path.read_text())
            previous = {
                str(key): [int(value[0]), int(value[1])]
                for key, value in decoded.get("objects", {}).items()
                if isinstance(value, list) and len(value) == 2
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            previous = {}
    changed = [
        item for item in objects
        if previous.get(item.key) != [item.size, item.mtime_ns]
    ]
    # R2's endpoint is most reliable with a modest targeted upload fan-out.
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(changed)))) as executor:
        hashes = list(executor.map(lambda item: upload_object(r2_client, config, item), changed))
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    # Persist the exact remotely committed pointer so full R2 reconciliation
    # can protect and republish it.  Replace atomically only after every
    # referenced raster upload succeeded.
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=root,
        prefix=".live-edge-",
        suffix=".json",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(root / LIVE_EDGE_KEY)
    upload_catalog(r2_client, config, encoded, key=LIVE_EDGE_KEY)
    if state_path is not None:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_payload = json.dumps({
            "schemaVersion": 1,
            "generatedAt": payload["generatedAt"],
            "objects": {
                item.key: [item.size, item.mtime_ns]
                for item in objects
            },
        }, separators=(",", ":")).encode()
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=state_path.parent,
            prefix=f".{state_path.name}-",
            delete=False,
        ) as handle:
            state_temporary = Path(handle.name)
            handle.write(state_payload)
            handle.flush()
            os.fsync(handle.fileno())
        state_temporary.replace(state_path)
    return {
        "status": "published",
        "generatedAt": payload["generatedAt"],
        "objects": len(objects),
        "uploadedObjects": len(changed),
        "bytes": sum(item.size for item in objects),
        "hashes": len(hashes),
        "url": f"{config.public_base_url}/{LIVE_EDGE_KEY}" if config.public_base_url else LIVE_EDGE_KEY,
    }
