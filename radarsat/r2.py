from __future__ import annotations

import datetime as dt
import hashlib
import json
import mimetypes
import os
import random
import re
import shutil
import sqlite3
import stat as stat_module
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import DOMAINS
from .geomet import format_utc
from .pipeline import write_status
from .retention import keep_layer_frame
from .westwx_catalog import build_westwx_catalog


UTC = dt.timezone.utc
KEYCHAIN_ACCOUNT = "radar-sat"
KEYCHAIN_SERVICES = {
    "account_id": "radar-sat-r2-account-id",
    "access_key_id": "radar-sat-r2-access-key-id",
    "secret_access_key": "radar-sat-r2-secret-access-key",
    "bucket": "radar-sat-r2-bucket",
    "public_base_url": "radar-sat-r2-public-base-url",
}
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
MUTABLE_CACHE_CONTROL = "public, max-age=60, must-revalidate"
STATIC_CACHE_CONTROL = "public, max-age=3600, stale-while-revalidate=86400"
CATALOG_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"
LIVE_EDGE_KEY = "live-edge.json"
DEFAULT_WARN_BYTES = 6_500_000_000
DEFAULT_MAX_BYTES = 8_000_000_000
# R2 begins leaving requests pending when a single Mac opens two dozen PUTs at
# once.  Twelve still fills the uplink, while avoiding that long-tail stall.
UPLOAD_WORKERS = 12
STAMP_RE = re.compile(r"^(\d{8}T\d{4}Z)$")
VIDEO_GENERATION_RE = re.compile(r"^(\d{8}T\d{4}Z)-([0-9a-f]{12})$")
VIDEO_TRACKS = frozenset({"live", "archive"})
VIDEO_MIN_GENERATIONS = 3
VIDEO_ORPHAN_GRACE = dt.timedelta(hours=1)
DEFAULT_COMPOSITE_PRESET_ID = "operational-default-v1"
DEFAULT_COMPOSITE_PILOT_PROFILES = frozenset({
    ("bc-large-overlay", "raw-visir"),
    ("north-america-overlay", "westwx-visir"),
})
VIDEO_IMMUTABLE_PREFIXES = (
    "videos/",
    "video-segments/",
    "video-manifests/",
    "video-proxies/",
    "video-static-overlays/",
)


class R2ConfigurationError(RuntimeError):
    pass


class PublicationSafetyError(RuntimeError):
    pass


def keychain_password(service: str, account: str = KEYCHAIN_ACCOUNT) -> str:
    security = shutil.which("security")
    if security is None:
        return ""
    result = subprocess.run(
        [security, "find-generic-password", "-a", account, "-s", service, "-w"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def configured_value(environment_name: str, keychain_name: str) -> str:
    value = os.environ.get(environment_name, "").strip()
    return value or keychain_password(KEYCHAIN_SERVICES[keychain_name])


@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    bucket: str = "radar-sat"
    public_base_url: str = ""
    endpoint_override: str = ""
    warn_bytes: int = DEFAULT_WARN_BYTES
    max_bytes: int = DEFAULT_MAX_BYTES

    @classmethod
    def from_environment(cls) -> "R2Config":
        values = {
            "account_id": configured_value("RADARSAT_R2_ACCOUNT_ID", "account_id"),
            "access_key_id": configured_value("RADARSAT_R2_ACCESS_KEY_ID", "access_key_id"),
            "secret_access_key": configured_value(
                "RADARSAT_R2_SECRET_ACCESS_KEY", "secret_access_key"
            ),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise R2ConfigurationError(
                "Missing Radar-Sat R2 configuration: " + ", ".join(missing)
            )
        bucket = configured_value("RADARSAT_R2_BUCKET", "bucket") or "radar-sat"
        public_base_url = configured_value(
            "RADARSAT_R2_PUBLIC_BASE_URL", "public_base_url"
        ).rstrip("/")
        warn_bytes = int(os.environ.get("RADARSAT_R2_WARN_BYTES", DEFAULT_WARN_BYTES))
        max_bytes = int(os.environ.get("RADARSAT_R2_MAX_BYTES", DEFAULT_MAX_BYTES))
        if warn_bytes <= 0 or max_bytes <= 0 or warn_bytes > max_bytes:
            raise R2ConfigurationError(
                "RADARSAT_R2_WARN_BYTES and RADARSAT_R2_MAX_BYTES must be positive, "
                "with warning <= maximum"
            )
        return cls(
            **values,
            bucket=bucket,
            public_base_url=public_base_url,
            endpoint_override=os.environ.get("RADARSAT_R2_ENDPOINT_URL", "").strip(),
            warn_bytes=warn_bytes,
            max_bytes=max_bytes,
        )

    @property
    def endpoint_url(self) -> str:
        return self.endpoint_override or (
            f"https://{self.account_id}.r2.cloudflarestorage.com"
        )


@dataclass(frozen=True)
class LocalObject:
    key: str
    path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class RemoteInventory:
    sizes: dict[str, int]
    modified_at: dict[str, dt.datetime]


class PublishState:
    def __init__(self, path: Path, scope: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS objects (
                object_key TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        previous = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'scope'"
        ).fetchone()
        if previous is not None and str(previous[0]) != scope:
            self.connection.execute("DELETE FROM objects")
        self.connection.execute(
            "INSERT INTO metadata(key, value) VALUES('scope', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (scope,),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def unchanged(self, item: LocalObject) -> bool:
        row = self.connection.execute(
            "SELECT size_bytes, mtime_ns FROM objects WHERE object_key = ?", (item.key,)
        ).fetchone()
        return bool(
            row is not None
            and int(row["size_bytes"]) == item.size
            and int(row["mtime_ns"]) == item.mtime_ns
        )

    def record(self, item: LocalObject, sha256: str) -> None:
        self.connection.execute(
            """
            INSERT INTO objects(object_key, size_bytes, mtime_ns, sha256, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(object_key) DO UPDATE SET
                size_bytes = excluded.size_bytes,
                mtime_ns = excluded.mtime_ns,
                sha256 = excluded.sha256,
                uploaded_at = excluded.uploaded_at
            """,
            (
                item.key,
                item.size,
                item.mtime_ns,
                sha256,
                format_utc(dt.datetime.now(UTC)),
            ),
        )
        self.connection.commit()

    def forget(self, keys: Iterable[str]) -> None:
        self.connection.executemany(
            "DELETE FROM objects WHERE object_key = ?", ((key,) for key in keys)
        )
        self.connection.commit()

    def known_sizes(self) -> dict[str, int]:
        """Return sizes of objects confirmed by successful prior uploads."""
        return {
            key: values[0]
            for key, values in self.known_objects().items()
        }

    def known_objects(self) -> dict[str, tuple[int, int]]:
        """Load successful-upload size/mtime state in one SQLite query."""
        return {
            str(row["object_key"]): (
                int(row["size_bytes"]),
                int(row["mtime_ns"]),
            )
            for row in self.connection.execute(
                "SELECT object_key, size_bytes, mtime_ns FROM objects"
            )
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_local_object(
    root: Path,
    root_resolved: Path,
    relative: str,
    resolved_parents: dict[str, Path],
) -> LocalObject:
    if not relative or relative.startswith("/"):
        raise PublicationSafetyError(f"Unsafe catalog path: {relative!r}")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise PublicationSafetyError(f"Catalog path escapes output root: {relative!r}")
    parent_key = relative_path.parent.as_posix()
    parent = resolved_parents.get(parent_key)
    if parent is None:
        parent = (root / relative_path.parent).resolve()
        if not parent.is_relative_to(root_resolved):
            raise PublicationSafetyError(f"Catalog path escapes output root: {relative!r}")
        resolved_parents[parent_key] = parent
    candidate = parent / relative_path.name
    try:
        # ``lstat`` both rejects a final symlink and supplies size/mtime in one
        # filesystem call. Parent directories are resolved and containment-
        # checked once per directory above.
        item_stat = candidate.lstat()
    except OSError as error:
        raise PublicationSafetyError(
            f"Catalog asset is missing or unreadable: {relative}"
        ) from error
    if not stat_module.S_ISREG(item_stat.st_mode) or item_stat.st_size <= 0:
        raise PublicationSafetyError(f"Catalog asset is missing or empty: {relative}")
    return LocalObject(
        key=relative,
        path=candidate,
        size=item_stat.st_size,
        mtime_ns=item_stat.st_mtime_ns,
    )


def _metadata_path_for_frame(root: Path, frame_key: str) -> Path:
    parts = Path(frame_key).parts
    if not parts or parts[0] != "frames":
        raise PublicationSafetyError(f"Unexpected frame path: {frame_key}")
    return root.joinpath("metadata", *parts[1:]).with_suffix(".json")


def _relative_file_available(root: Path, relative: str) -> bool:
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        return False
    try:
        stat = (root / path).lstat()
    except OSError:
        return False
    return stat_module.S_ISREG(stat.st_mode) and stat.st_size > 0


def _tile_manifest_paths(root: Path, relative: str) -> list[str]:
    manifest_relative = Path(relative)
    if (
        manifest_relative.is_absolute()
        or not manifest_relative.parts
        or ".." in manifest_relative.parts
    ):
        raise PublicationSafetyError(f"Unsafe tile manifest path: {relative!r}")
    root_resolved = root.resolve()
    manifest = (root / manifest_relative).resolve()
    if not manifest.is_relative_to(root_resolved) or not manifest.is_file():
        raise PublicationSafetyError(f"Tile manifest is missing: {relative!r}")
    try:
        payload = json.loads(manifest.read_bytes())
    except json.JSONDecodeError as error:
        raise PublicationSafetyError(
            f"Tile manifest is not valid JSON: {relative!r}"
        ) from error
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise PublicationSafetyError(f"Tile manifest contains no files: {relative!r}")
    return [str(value) for value in files]


def _safe_relative_value(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or path.as_posix() != value
    ):
        return None
    return value


def _video_manifest_components(
    product_id: str,
    layer_id: str,
    track: str,
    pointer: object,
) -> tuple[str, str] | None:
    if not isinstance(pointer, Mapping):
        return None
    generation = pointer.get("generation")
    manifest_value = _safe_relative_value(pointer.get("manifestPath"))
    if (
        not isinstance(generation, str)
        or not VIDEO_GENERATION_RE.fullmatch(generation)
        or track not in VIDEO_TRACKS
        or manifest_value is None
    ):
        return None
    expected = (
        "video-manifests",
        product_id,
        layer_id,
        track,
        f"{generation}.json",
    )
    if Path(manifest_value).parts != expected:
        return None
    return generation, manifest_value


def _video_asset_available(
    root: Path,
    relative: str,
    declared_bytes: object = None,
) -> bool:
    if not _relative_file_available(root, relative):
        return False
    try:
        if not (root / relative).resolve().is_relative_to(root.resolve()):
            return False
    except OSError:
        return False
    if declared_bytes is None:
        return True
    if (
        not isinstance(declared_bytes, int)
        or isinstance(declared_bytes, bool)
        or declared_bytes <= 0
    ):
        return False
    try:
        return (root / relative).stat().st_size == declared_bytes
    except OSError:
        return False


def _proxy_paths(root: Path, product_id: str, proxies: object) -> list[str] | None:
    if proxies is None:
        return []
    if isinstance(proxies, Mapping):
        values = list(proxies.values())
    elif isinstance(proxies, list):
        values = proxies
    else:
        return None
    paths: list[str] = []
    for proxy in values:
        if not isinstance(proxy, Mapping):
            return None
        relative = _safe_relative_value(proxy.get("path"))
        if relative is None:
            return None
        parts = Path(relative).parts
        if (
            len(parts) != 4
            or parts[0] != "video-proxies"
            or parts[1] != product_id
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", parts[2])
            or not re.fullmatch(r"[0-9a-f]{16}\.(?:webp|png)", parts[3])
            or not _video_asset_available(root, relative, proxy.get("byteLength"))
        ):
            return None
        paths.append(relative)
    return paths


def _static_overlay_path(root: Path, product_id: str, value: object) -> str | None:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        relative = _safe_relative_value(value.get("path"))
        declared_bytes = value.get("byteLength")
    else:
        relative = _safe_relative_value(value)
        declared_bytes = None
    if relative is None:
        return None
    parts = Path(relative).parts
    proxy_shape = (
        len(parts) == 4
        and parts[0] == "video-proxies"
        and parts[1] == product_id
        and re.fullmatch(r"[a-z0-9][a-z0-9-]*", parts[2]) is not None
        and re.fullmatch(r"[0-9a-f]{16}\.(?:webp|png)", parts[3]) is not None
    )
    static_shape = (
        len(parts) == 3
        and parts[0] == "video-static-overlays"
        and parts[1] == product_id
        and re.fullmatch(r"[0-9a-f]{16}\.png", parts[2]) is not None
    )
    if not (proxy_shape or static_shape) or not _video_asset_available(
        root,
        relative,
        declared_bytes,
    ):
        return None
    return relative


def _default_composite_paths(
    root: Path,
    product_id: str,
    layer_id: str,
    track: str,
    payload: Mapping[str, object],
) -> list[str]:
    """Validate an optional fully composited HLS preset.

    The regular satellite video and proxy overlays remain the authoritative
    fallback. A malformed or torn optional preset therefore contributes no
    publication dependencies, rather than invalidating the complete base
    video profile. The browser applies the same fail-open rule when reading
    the immutable manifest.
    """
    value = payload.get("defaultComposite")
    if value is None:
        return []
    if (
        (product_id, layer_id) not in DEFAULT_COMPOSITE_PILOT_PROFILES
        or not isinstance(value, Mapping)
        or value.get("id") != DEFAULT_COMPOSITE_PRESET_ID
        or payload.get("transport") != "hls-ts"
    ):
        return []
    layer_ids = value.get("layerIds")
    if (
        not isinstance(layer_ids, list)
        or not layer_ids
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]*", item) is None
            for item in layer_ids
        )
        or len(set(layer_ids)) != len(layer_ids)
    ):
        return []
    # The composited raster must use the same display crop as the manifest;
    # otherwise toggling to the dynamic proxy fallback would move the map.
    if value.get("mediaViewport") != payload.get("viewport"):
        return []
    media = value.get("media")
    if not isinstance(media, Mapping):
        return []
    media_relative = _safe_relative_value(media.get("path"))
    media_parts = Path(media_relative).parts if media_relative is not None else ()
    owner = f"composite-{product_id}"
    media_width = media.get("width")
    media_height = media.get("height")
    content_height = media.get("contentHeight", media_height)
    if (
        media_relative is None
        or media_parts[:4] != ("videos", owner, layer_id, track)
        or len(media_parts) != 5
        or Path(media_parts[4]).suffix != ".m3u8"
        or not VIDEO_GENERATION_RE.fullmatch(Path(media_parts[4]).stem)
        or media.get("mimeType") != "application/vnd.apple.mpegurl"
        or media.get("codec") != "avc1"
        or not isinstance(media_width, int)
        or isinstance(media_width, bool)
        or media_width <= 0
        or media_width != payload.get("width")
        or not isinstance(media_height, int)
        or isinstance(media_height, bool)
        or media_height <= 0
        or not isinstance(content_height, int)
        or isinstance(content_height, bool)
        or content_height <= 0
        or content_height > media_height
        or content_height != payload.get("height")
        or not isinstance(media.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(media.get("sha256"))) is None
        or not _video_asset_available(root, media_relative, media.get("byteLength"))
    ):
        return []
    segments = media.get("segments")
    if not isinstance(segments, list) or not segments:
        return []
    segment_paths: list[str] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            return []
        segment_relative = _safe_relative_value(segment.get("path"))
        segment_parts = (
            Path(segment_relative).parts if segment_relative is not None else ()
        )
        if (
            segment_relative is None
            or len(segment_parts) != 5
            or segment_parts[:4]
            != ("video-segments", owner, layer_id, track)
            or re.fullmatch(
                r"\d{8}T\d{4}Z-[0-9a-f]{16}\.ts", segment_parts[4]
            )
            is None
            or not isinstance(segment.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(segment.get("sha256"))) is None
            or not _video_asset_available(
                root,
                segment_relative,
                segment.get("byteLength"),
            )
        ):
            return []
        segment_paths.append(segment_relative)
    try:
        playlist_lines = (root / media_relative).read_text().splitlines()
        playlist_uris = [
            line.strip()
            for line in playlist_lines
            if line.strip() and not line.lstrip().startswith("#")
        ]
        playlist_root = (root / media_relative).parent
        referenced_segments = [
            (playlist_root / uri).resolve().relative_to(root.resolve()).as_posix()
            for uri in playlist_uris
        ]
    except (OSError, UnicodeDecodeError, ValueError):
        return []
    if referenced_segments != segment_paths:
        return []
    return [media_relative, *segment_paths]


def _video_manifest_paths(
    root: Path,
    product_id: str,
    layer_id: str,
    track: str,
    pointer: object,
) -> list[str] | None:
    components = _video_manifest_components(product_id, layer_id, track, pointer)
    if components is None:
        return None
    generation, manifest_relative = components
    if not _video_asset_available(root, manifest_relative):
        return None
    try:
        payload = json.loads((root / manifest_relative).read_bytes())
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, Mapping)
        or payload.get("schemaVersion") != 1
        or payload.get("generation") != generation
        or payload.get("productId") != product_id
        or payload.get("layerId") != layer_id
        or payload.get("track") != track
        or payload.get("transport") not in {"progressive-mp4", "hls-ts"}
    ):
        return None
    frames = payload.get("frames")
    if not isinstance(frames, list) or len(frames) < 2:
        return None
    media = payload.get("media")
    if not isinstance(media, Mapping):
        return None
    media_relative = _safe_relative_value(media.get("path"))
    media_parts = Path(media_relative).parts if media_relative is not None else ()
    domain_id = payload.get("domainId")
    media_owner_ok = (
        len(media_parts) == 5
        and (
            media_parts[:4] == ("videos", product_id, layer_id, track)
            or (
                isinstance(domain_id, str)
                and media_parts[0] == "videos"
                and re.fullmatch(
                    rf"shared-{re.escape(domain_id)}-[a-z0-9][a-z0-9-]*",
                    media_parts[1],
                )
                is not None
                and media_parts[2:4] == (layer_id, track)
            )
        )
    )
    transport = payload.get("transport")
    expected_suffix = ".mp4" if transport == "progressive-mp4" else ".m3u8"
    expected_mime = (
        "video/mp4"
        if transport == "progressive-mp4"
        else "application/vnd.apple.mpegurl"
    )
    if (
        media_relative is None
        or not media_owner_ok
        or Path(media_parts[4]).suffix != expected_suffix
        or not VIDEO_GENERATION_RE.fullmatch(Path(media_parts[4]).stem)
        or media.get("mimeType") != expected_mime
        or not _video_asset_available(root, media_relative, media.get("byteLength"))
    ):
        return None
    segment_paths: list[str] = []
    if transport == "hls-ts":
        segments = media.get("segments")
        if not isinstance(segments, list) or not segments:
            return None
        for segment in segments:
            if not isinstance(segment, Mapping):
                return None
            segment_relative = _safe_relative_value(segment.get("path"))
            segment_parts = (
                Path(segment_relative).parts if segment_relative is not None else ()
            )
            if (
                segment_relative is None
                or len(segment_parts) != 5
                or segment_parts[:4]
                != ("video-segments", media_parts[1], layer_id, track)
                or re.fullmatch(
                    r"\d{8}T\d{4}Z-[0-9a-f]{16}\.ts", segment_parts[4]
                )
                is None
                or not _video_asset_available(
                    root, segment_relative, segment.get("byteLength")
                )
            ):
                return None
            segment_paths.append(segment_relative)
    proxy_paths = _proxy_paths(root, product_id, payload.get("proxies"))
    if proxy_paths is None:
        return None
    proxies = payload.get("proxies")
    assert isinstance(proxies, Mapping)
    proxy_keys = {key for key in proxies if isinstance(key, str)}
    for index, frame in enumerate(frames):
        if not isinstance(frame, Mapping) or frame.get("index") != index:
            return None
        selections = frame.get("proxyLayers")
        if not isinstance(selections, list):
            return None
        for selection in selections:
            if (
                not isinstance(selection, Mapping)
                or not isinstance(selection.get("id"), str)
                or not isinstance(selection.get("renderId"), str)
                or selection.get("sourceKey") not in proxy_keys
                or (
                    selection.get("sourceValidTime") is not None
                    and not isinstance(selection.get("sourceValidTime"), str)
                )
            ):
                return None
    static_path = _static_overlay_path(root, product_id, payload.get("staticOverlay"))
    if static_path is None:
        return None
    paths = [manifest_relative, media_relative, *segment_paths, *proxy_paths]
    if static_path:
        paths.append(static_path)
    # A corrupt optional composite is deliberately omitted while the valid
    # base media/proxy profile remains publishable.
    paths.extend(
        _default_composite_paths(
            root,
            product_id,
            layer_id,
            track,
            payload,
        )
    )
    return paths


def _sanitize_video_profiles(
    root: Path,
    catalog: dict[str, Any],
    *,
    existing_video_keys: set[str] | None = None,
) -> set[str]:
    """Keep only complete optional video profiles and return their assets."""
    source = catalog.get("videoProfiles")
    if not isinstance(source, Mapping):
        catalog.pop("videoProfiles", None)
        return set()
    known_layers: dict[str, set[str]] = {}
    products = catalog.get("products")
    if isinstance(products, list):
        for product in products:
            if not isinstance(product, Mapping) or not isinstance(product.get("id"), str):
                continue
            layers = product.get("layers")
            if not isinstance(layers, list):
                continue
            known_layers[str(product["id"])] = {
                str(layer["id"])
                for layer in layers
                if isinstance(layer, Mapping) and isinstance(layer.get("id"), str)
            }
    sanitized: dict[str, dict[str, dict[str, object]]] = {}
    relative_paths: set[str] = set()
    for product_id, layers in source.items():
        if not isinstance(product_id, str) or not isinstance(layers, Mapping):
            continue
        for layer_id, tracks in layers.items():
            if (
                not isinstance(layer_id, str)
                or layer_id not in known_layers.get(product_id, set())
                or not isinstance(tracks, Mapping)
            ):
                continue
            for track, pointer in tracks.items():
                if not isinstance(track, str):
                    continue
                candidates = [pointer]
                if existing_video_keys is not None:
                    manifest_dir = (
                        root / "video-manifests" / product_id / layer_id / track
                    )
                    try:
                        manifests = sorted(
                            manifest_dir.glob("*.json"),
                            key=lambda path: path.stat().st_mtime_ns,
                            reverse=True,
                        )
                    except OSError:
                        manifests = []
                    candidates.extend(
                        {
                            "generation": manifest.stem,
                            "manifestPath": manifest.relative_to(root).as_posix(),
                        }
                        for manifest in manifests
                    )
                selected: tuple[str, str, list[str]] | None = None
                seen: set[str] = set()
                for candidate in candidates:
                    components = _video_manifest_components(
                        product_id,
                        layer_id,
                        track,
                        candidate,
                    )
                    if components is None or components[1] in seen:
                        continue
                    seen.add(components[1])
                    # Immutable manifests are the root of each dependency
                    # bundle. If this root was never recorded as uploaded,
                    # reject it before parsing and statting hundreds of proxy
                    # and segment paths.
                    if (
                        existing_video_keys is not None
                        and components[1] not in existing_video_keys
                    ):
                        continue
                    paths = _video_manifest_paths(
                        root,
                        product_id,
                        layer_id,
                        track,
                        candidate,
                    )
                    if paths is None:
                        continue
                    if existing_video_keys is not None and not set(paths).issubset(
                        existing_video_keys
                    ):
                        continue
                    selected = (*components, paths)
                    break
                if selected is None:
                    continue
                generation, manifest_path, paths = selected
                sanitized.setdefault(product_id, {}).setdefault(layer_id, {})[track] = {
                    "generation": generation,
                    "manifestPath": manifest_path,
                }
                relative_paths.update(paths)
    if sanitized:
        catalog["videoProfiles"] = sanitized
    else:
        catalog.pop("videoProfiles", None)
    return relative_paths


def discover_objects(
    root: Path,
    *,
    whole_frame_only: bool = False,
    minimum_valid_time: dt.datetime | None = None,
    existing_video_keys: set[str] | None = None,
) -> tuple[list[LocalObject], bytes]:
    """Return every object referenced by the catalog plus its metadata.

    ``catalog.json`` is returned separately so the publisher can upload it only
    after every referenced asset succeeds.
    """
    catalog_path = root / "catalog.json"
    if not catalog_path.is_file():
        raise PublicationSafetyError(f"Missing catalog: {catalog_path}")
    catalog_bytes = catalog_path.read_bytes()
    try:
        catalog = json.loads(catalog_bytes)
    except json.JSONDecodeError as error:
        raise PublicationSafetyError("catalog.json is not valid JSON") from error
    # Ingest and retention intentionally run while publication is pending.
    # Normalize the catalog against files that still exist, and degrade a
    # broken optional tile pyramid to its whole-frame fallback.
    for domain in catalog.get("domains", {}).values():
        for layer in domain.get("layers", {}).values():
            retained_frames: list[dict[str, object]] = []
            for value in layer.get("frames", []):
                if not isinstance(value, dict):
                    continue
                frame = dict(value)
                if minimum_valid_time is not None:
                    try:
                        valid_time = dt.datetime.fromisoformat(
                            str(frame["validTime"]).replace("Z", "+00:00")
                        ).astimezone(UTC)
                    except (KeyError, ValueError):
                        continue
                    if valid_time < minimum_valid_time:
                        continue
                key = str(frame.get("path", ""))
                try:
                    metadata = _metadata_path_for_frame(root, key)
                    metadata_relative = metadata.relative_to(root).as_posix()
                except (ValueError, PublicationSafetyError):
                    continue
                if (
                    not _relative_file_available(root, key)
                    or not _relative_file_available(root, metadata_relative)
                ):
                    continue
                tiles = frame.get("tiles")
                if whole_frame_only:
                    frame.pop("tiles", None)
                elif isinstance(tiles, dict) and isinstance(tiles.get("manifest"), str):
                    try:
                        tile_files = _tile_manifest_paths(root, str(tiles["manifest"]))
                        tile_ready = all(
                            _relative_file_available(root, relative)
                            for relative in tile_files
                        )
                    except PublicationSafetyError:
                        tile_ready = False
                    if not tile_ready:
                        frame.pop("tiles", None)
                retained_frames.append(frame)
            layer["frames"] = retained_frames
    # Video is an optional acceleration path. A torn encoder output must never
    # block publication of the complete image archive: validate every
    # immutable dependency and strip only an incomplete video pointer.
    video_paths = _sanitize_video_profiles(
        root,
        catalog,
        existing_video_keys=existing_video_keys,
    )
    catalog_bytes = json.dumps(catalog, separators=(",", ":")).encode()

    relative_paths: set[str] = set()
    frame_count = 0
    for domain in catalog.get("domains", {}).values():
        for layer in domain.get("layers", {}).values():
            for frame in layer.get("frames", []):
                key = str(frame.get("path", ""))
                relative_paths.add(key)
                metadata_path = _metadata_path_for_frame(root, key)
                relative_paths.add(metadata_path.relative_to(root).as_posix())
                frame_count += 1
                tiles = frame.get("tiles")
                if isinstance(tiles, dict) and isinstance(tiles.get("manifest"), str):
                    manifest = str(tiles["manifest"])
                    relative_paths.add(manifest)
                    relative_paths.update(
                        _tile_manifest_paths(root, manifest)
                    )
        for static in domain.get("staticLayers", {}).values():
            relative_paths.add(str(static.get("path", "")))
    for legend in catalog.get("legends", {}).values():
        if legend.get("path"):
            relative_paths.add(str(legend["path"]))
    relative_paths.update(video_paths)
    # The latency-sensitive radar/lightning publisher maintains this small
    # pointer independently of the large catalog.  Include the local copy in
    # ordinary reconciliation so a later full sync preserves it instead of
    # treating it as an unreferenced remote object.
    if (root / LIVE_EDGE_KEY).is_file():
        relative_paths.add(LIVE_EDGE_KEY)

    if frame_count == 0:
        raise PublicationSafetyError("Refusing to publish a catalog containing zero frames")

    root_resolved = root.resolve()
    resolved_parents: dict[str, Path] = {}
    objects = [
        _safe_local_object(root, root_resolved, key, resolved_parents)
        for key in sorted(relative_paths)
    ]
    return objects, catalog_bytes


def build_catalog_index(catalog_bytes: bytes) -> bytes:
    """Build the small, frequently-polled view of a published catalog.

    Video manifests contain the authoritative frame/proxy selections for the
    smooth playback path.  The browser only needs the newest ordinary image
    frame per layer to determine product availability and to ensure that a
    video generation is current.  The complete image history remains at
    ``catalog.json`` and is fetched only when video is unavailable or fails.
    """
    try:
        catalog = json.loads(catalog_bytes)
    except json.JSONDecodeError as error:
        raise PublicationSafetyError("Published catalog is not valid JSON") from error
    if not isinstance(catalog, dict):
        raise PublicationSafetyError("Published catalog must be a JSON object")
    for domain in catalog.get("domains", {}).values():
        if not isinstance(domain, dict):
            continue
        for layer in domain.get("layers", {}).values():
            if not isinstance(layer, dict):
                continue
            frames = [
                frame for frame in layer.get("frames", [])
                if isinstance(frame, dict)
            ]
            layer["frames"] = [
                max(frames, key=lambda frame: str(frame.get("validTime", "")))
            ] if frames else []
    catalog["catalogMode"] = "index"
    catalog["fullCatalogPath"] = "catalog.json"
    return json.dumps(catalog, separators=(",", ":")).encode()


def publication_snapshot(
    root: Path,
    state_path: Path,
    *,
    whole_frame_only: bool,
    minimum_valid_time: dt.datetime | None,
    known_objects: Mapping[str, tuple[int, int]] | None = None,
    existing_video_keys: set[str] | None = None,
    initial_discovery: tuple[list[LocalObject], bytes] | None = None,
    attempts: int = 4,
) -> tuple[Path, list[LocalObject], bytes]:
    """Hard-link the upload set while live retention continues.

    A fast publication already trusts its durable successful-upload index.
    Objects unchanged in that index are never read during the PUT phase, so
    leave those in place and snapshot only new or modified objects. This keeps
    a current-catalog commit fast even when the retained archive has tens of
    thousands of files.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in state_path.parent.glob("r2-publish-snapshot-*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
    last_error: Exception | None = None
    for attempt in range(attempts):
        snapshot_root = Path(
            tempfile.mkdtemp(prefix="r2-publish-snapshot-", dir=state_path.parent)
        )
        try:
            if attempt == 0 and initial_discovery is not None:
                objects, catalog_bytes = initial_discovery
            else:
                objects, catalog_bytes = discover_objects(
                    root,
                    whole_frame_only=whole_frame_only,
                    minimum_valid_time=minimum_valid_time,
                    existing_video_keys=existing_video_keys,
                )
            snapshot_objects: list[LocalObject] = []
            for item in objects:
                if known_objects is not None and known_objects.get(item.key) == (
                    item.size,
                    item.mtime_ns,
                ):
                    snapshot_objects.append(item)
                    continue
                destination = snapshot_root / item.key
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(item.path, destination)
                except OSError:
                    # Preserve portability when output/state roots are placed
                    # on separate filesystems. The normal configuration uses
                    # hard links and therefore consumes no duplicate blocks.
                    shutil.copy2(item.path, destination)
                snapshot_objects.append(
                    LocalObject(
                        key=item.key,
                        path=destination,
                        size=item.size,
                        mtime_ns=item.mtime_ns,
                    )
                )
            return snapshot_root, snapshot_objects, catalog_bytes
        except (OSError, PublicationSafetyError) as error:
            last_error = error
            shutil.rmtree(snapshot_root, ignore_errors=True)
    raise PublicationSafetyError(
        f"Could not capture a stable publication snapshot: {last_error}"
    )


def boto3_client(config: R2Config):
    try:
        import boto3
        from botocore.config import Config
    except ImportError as error:
        raise RuntimeError("boto3 is required for R2 publication") from error
    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        region_name="auto",
        config=Config(
            retries={"max_attempts": 1},
            connect_timeout=15,
            read_timeout=90,
            max_pool_connections=UPLOAD_WORKERS,
        ),
    )


def retry(operation: Any, description: str, attempts: int = 5) -> Any:
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception:
            if attempt == attempts:
                raise
            delay = min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.35)
            print(
                f"{description} failed; retrying in {delay:.1f}s ({attempt}/{attempts})",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


def _as_utc(value: dt.datetime) -> dt.datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


def list_remote_inventory(client: Any, bucket: str) -> RemoteInventory:
    objects: dict[str, int] = {}
    modified_at: dict[str, dt.datetime] = {}
    token: str | None = None
    while True:
        arguments: dict[str, Any] = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            arguments["ContinuationToken"] = token
        response = retry(
            lambda: client.list_objects_v2(**arguments),
            f"List R2 bucket {bucket}",
        )
        for item in response.get("Contents", []):
            key = str(item["Key"])
            objects[key] = int(item.get("Size", 0))
            modified = item.get("LastModified")
            if isinstance(modified, dt.datetime):
                modified_at[key] = _as_utc(modified)
        if not response.get("IsTruncated"):
            break
        token = str(response.get("NextContinuationToken", ""))
        if not token:
            raise RuntimeError("R2 returned a truncated listing without a continuation token")
    return RemoteInventory(objects, modified_at)


def list_remote_objects(client: Any, bucket: str) -> dict[str, int]:
    """Compatibility wrapper for callers interested only in object sizes."""
    return list_remote_inventory(client, bucket).sizes


def content_type(path: Path) -> str:
    if path.suffix.lower() == ".mp4":
        return "video/mp4"
    if path.suffix.lower() == ".m3u8":
        return "application/vnd.apple.mpegurl"
    if path.suffix.lower() == ".ts":
        return "video/mp2t"
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed == "application/json":
        return "application/json; charset=utf-8"
    return guessed or "application/octet-stream"


def cache_control(key: str) -> str:
    if key == LIVE_EDGE_KEY:
        return CATALOG_CACHE_CONTROL
    if key.startswith(VIDEO_IMMUTABLE_PREFIXES):
        return IMMUTABLE_CACHE_CONTROL
    if key.startswith(("frames/", "metadata/")):
        # Same-valid-time native frames and derived trails can be corrected in
        # place, so these keys must never be advertised as immutable.
        return MUTABLE_CACHE_CONTROL
    return STATIC_CACHE_CONTROL


def upload_object(client: Any, config: R2Config, item: LocalObject) -> str:
    sha256 = sha256_file(item.path)

    def put() -> Any:
        with item.path.open("rb") as body:
            return client.put_object(
                Bucket=config.bucket,
                Key=item.key,
                Body=body,
                ContentType=content_type(item.path),
                CacheControl=cache_control(item.key),
                Metadata={"sha256": sha256},
            )

    retry(put, f"Upload {item.key}")
    return sha256


def upload_catalog(client: Any, config: R2Config, payload: bytes, key: str = "catalog.json") -> None:
    retry(
        lambda: client.put_object(
            Bucket=config.bucket,
            Key=key,
            Body=payload,
            ContentType="application/json; charset=utf-8",
            CacheControl=CATALOG_CACHE_CONTROL,
        ),
        f"Upload {key}",
    )


def remote_valid_time(key: str) -> tuple[dt.datetime, str] | None:
    parts = Path(key).parts
    if len(parts) < 7 or parts[0] not in {
        "frames",
        "metadata",
        "tiles",
        "tile-manifests",
    }:
        return None
    domain_id = parts[1]
    stamp = next(
        (Path(part).stem for part in reversed(parts) if STAMP_RE.fullmatch(Path(part).stem)),
        "",
    )
    if domain_id not in DOMAINS or not STAMP_RE.fullmatch(stamp):
        return None
    try:
        value = dt.datetime.strptime(stamp, "%Y%m%dT%H%MZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return value, DOMAINS[domain_id].tier


def expired_remote_keys(remote: Mapping[str, int], now: dt.datetime) -> list[str]:
    expired: list[str] = []
    for key in remote:
        parsed = remote_valid_time(key)
        if parsed is None:
            continue
        valid_time, tier = parsed
        parts = Path(key).parts
        layer_id = parts[2] if len(parts) > 2 else ""
        if not keep_layer_frame(valid_time, now, tier, layer_id):
            expired.append(key)
    return sorted(expired)


def _video_generation_key(key: str) -> tuple[tuple[str, str, str, str], str] | None:
    parts = Path(key).parts
    if (
        len(parts) != 5
        or parts[0] not in {"videos", "video-manifests"}
        or parts[3] not in VIDEO_TRACKS
    ):
        return None
    generation = Path(parts[4]).stem
    if not VIDEO_GENERATION_RE.fullmatch(generation):
        return None
    expected_suffixes = {".mp4", ".m3u8"} if parts[0] == "videos" else {".json"}
    if Path(parts[4]).suffix not in expected_suffixes:
        return None
    return (parts[0], parts[1], parts[2], parts[3]), generation


def _generation_time(generation: str) -> dt.datetime:
    match = VIDEO_GENERATION_RE.fullmatch(generation)
    if match is None:
        raise ValueError(f"Invalid video generation: {generation}")
    return dt.datetime.strptime(match.group(1), "%Y%m%dT%H%MZ").replace(tzinfo=UTC)


def expired_video_keys(
    remote: Mapping[str, int],
    now: dt.datetime,
    *,
    desired_keys: Iterable[str] = (),
    modified_at: Mapping[str, dt.datetime] | None = None,
) -> list[str]:
    """Select unreachable video generations after a browser-safe grace.

    For an active product/layer/track, the newest three generations survive
    regardless of age, protecting recovery after a long feed stall. A retired
    profile with no catalog-referenced object keeps only the one-hour browser
    grace; otherwise old secondary profiles would occupy R2 indefinitely.
    Catalog-referenced objects are always excluded from this post-commit pass.
    """
    desired = set(desired_keys)
    modifications = modified_at or {}
    groups: dict[tuple[str, str, str, str], dict[str, list[str]]] = {}
    for key in remote:
        parsed = _video_generation_key(key)
        if parsed is None:
            continue
        group, generation = parsed
        groups.setdefault(group, {}).setdefault(generation, []).append(key)

    cutoff = _as_utc(now) - VIDEO_ORPHAN_GRACE
    expired: set[str] = set()
    for generations in groups.values():
        active = any(key in desired for keys in generations.values() for key in keys)
        newest = (
            set(sorted(generations, reverse=True)[:VIDEO_MIN_GENERATIONS])
            if active
            else set()
        )
        for generation, keys in generations.items():
            if generation in newest or any(key in desired for key in keys):
                continue
            timestamps = [
                _as_utc(value)
                for key in keys
                if isinstance((value := modifications.get(key)), dt.datetime)
            ]
            last_changed = max(timestamps) if timestamps else _generation_time(generation)
            if last_changed <= cutoff:
                expired.update(key for key in keys if key not in desired)

    for key in remote:
        if key in desired or not key.startswith(
            ("video-proxies/", "video-static-overlays/", "video-segments/")
        ):
            continue
        modified = modifications.get(key)
        # Hash-only proxy names carry no reliable timestamp. If R2 did not
        # provide LastModified, retain them rather than risk deleting a live
        # content-addressed dependency.
        if isinstance(modified, dt.datetime) and _as_utc(modified) <= cutoff:
            expired.add(key)
    return sorted(expired)


def retained_local_video_keys(
    root: Path,
    remote: Mapping[str, int],
    *,
    desired_keys: Iterable[str] = (),
) -> set[str]:
    """Protect dependencies of the locally retained newest generations.

    The encoder keeps its newest three immutable manifests. Reading those
    sidecars lets remote cleanup preserve an old generation's shared proxy
    objects even when the proxy itself was uploaded more than one hour ago.
    No missing historical object is re-uploaded; this set only constrains the
    post-commit deletion pass.
    """
    desired = set(desired_keys)
    groups: dict[tuple[str, str, str, str], set[str]] = {}
    for key in remote:
        parsed = _video_generation_key(key)
        if parsed is None:
            continue
        group, generation = parsed
        groups.setdefault(group, set()).add(generation)
    retained: set[str] = set()
    for (prefix, product_id, layer_id, track), generations in groups.items():
        if prefix != "video-manifests":
            continue
        group_prefix = f"video-manifests/{product_id}/{layer_id}/{track}/"
        if not any(key.startswith(group_prefix) for key in desired):
            continue
        for generation in sorted(generations, reverse=True)[:VIDEO_MIN_GENERATIONS]:
            pointer = {
                "generation": generation,
                "manifestPath": (
                    f"video-manifests/{product_id}/{layer_id}/{track}/{generation}.json"
                ),
            }
            paths = _video_manifest_paths(
                root,
                product_id,
                layer_id,
                track,
                pointer,
            )
            if paths is not None:
                retained.update(paths)
    return retained


def delete_objects(client: Any, config: R2Config, keys: Iterable[str]) -> int:
    values = list(keys)
    deleted = 0
    for start in range(0, len(values), 1000):
        batch = values[start : start + 1000]
        response = retry(
            lambda batch=batch: client.delete_objects(
                Bucket=config.bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            ),
            f"Delete {len(batch)} expired R2 objects",
        )
        errors = response.get("Errors", [])
        if errors:
            raise RuntimeError(f"R2 failed to delete expired objects: {errors[:3]}")
        deleted += len(batch)
    return deleted


def size_guard(
    objects: Iterable[LocalObject],
    catalog_bytes: bytes,
    remote: Mapping[str, int],
    config: R2Config,
    pending_delete: Iterable[str] = (),
) -> dict[str, int]:
    values = list(objects)
    catalog_index_bytes = build_catalog_index(catalog_bytes)
    local_bytes = (
        sum(item.size for item in values)
        + len(catalog_bytes)
        + len(catalog_index_bytes)
    )
    remote_bytes = sum(remote.values())
    replaced_bytes = sum(remote.get(item.key, 0) for item in values) + remote.get(
        "catalog.json", 0
    ) + remote.get("catalog-index.json", 0)
    peak_projected_bytes = remote_bytes - replaced_bytes + local_bytes
    desired_keys = {item.key for item in values}
    pending_delete_bytes = sum(
        remote.get(key, 0)
        for key in set(pending_delete)
        if key not in desired_keys
    )
    projected_bytes = peak_projected_bytes - pending_delete_bytes
    if (
        local_bytes >= config.warn_bytes
        or projected_bytes >= config.warn_bytes
        or peak_projected_bytes >= config.warn_bytes
    ):
        print(
            "R2 storage warning: "
            f"retained={local_bytes / 1_000_000_000:.2f} GB, "
            f"projected bucket={projected_bytes / 1_000_000_000:.2f} GB, "
            f"temporary peak={peak_projected_bytes / 1_000_000_000:.2f} GB",
            file=sys.stderr,
            flush=True,
        )
    growth = projected_bytes > remote_bytes
    if projected_bytes > config.max_bytes and growth:
        raise PublicationSafetyError(
            "R2 publication paused by the storage guardrail: "
            f"projected {projected_bytes / 1_000_000_000:.2f} GB exceeds "
            f"{config.max_bytes / 1_000_000_000:.2f} GB"
        )
    return {
        "localBytes": local_bytes,
        "remoteBytes": remote_bytes,
        "projectedBytes": projected_bytes,
        "peakProjectedBytes": peak_projected_bytes,
        "pendingDeleteBytes": pending_delete_bytes,
    }


def publish(
    root: Path,
    config: R2Config,
    state_path: Path,
    status_path: Path,
    *,
    client: Any | None = None,
    sync_delete: bool = True,
    fast: bool = False,
    dry_run: bool = False,
    whole_frame_only: bool = False,
    recovery_hours: float | None = None,
    existing_video_only: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    if existing_video_only and not fast:
        raise ValueError("existing_video_only requires fast publication")
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    snapshot_root: Path | None = None
    minimum_valid_time = (
        now - dt.timedelta(hours=recovery_hours)
        if recovery_hours is not None
        else None
    )
    state = PublishState(state_path, f"{config.account_id}/{config.bucket}")
    try:
        known_objects = state.known_objects()
        existing_video_keys = set(known_objects) if existing_video_only else None
        preflight_discovery: tuple[list[LocalObject], bytes] | None = None
        if fast and not dry_run:
            # Refuse a known-over-cap rapid commit before creating thousands of
            # hard links for a snapshot that cannot be uploaded. The regular
            # reconciliation path still inventories R2 and can make space by
            # deleting expired objects after its atomic catalog commit.
            preflight_objects, preflight_catalog = discover_objects(
                root,
                whole_frame_only=whole_frame_only,
                minimum_valid_time=minimum_valid_time,
                existing_video_keys=existing_video_keys,
            )
            size_guard(
                preflight_objects,
                preflight_catalog,
                {key: values[0] for key, values in known_objects.items()},
                config,
            )
            preflight_discovery = (preflight_objects, preflight_catalog)
        if dry_run:
            objects, catalog_bytes = discover_objects(
                root,
                whole_frame_only=whole_frame_only,
                minimum_valid_time=minimum_valid_time,
                existing_video_keys=existing_video_keys,
            )
        else:
            snapshot_root, objects, catalog_bytes = publication_snapshot(
                root,
                state_path,
                whole_frame_only=whole_frame_only,
                minimum_valid_time=minimum_valid_time,
                known_objects=known_objects if fast else None,
                existing_video_keys=existing_video_keys,
                initial_discovery=preflight_discovery,
            )
        westwx_catalog_bytes = json.dumps(
            build_westwx_catalog(json.loads(catalog_bytes)),
            separators=(",", ":"),
        ).encode()
        catalog_index_bytes = build_catalog_index(catalog_bytes)
        client = client or boto3_client(config)
        # Rapid satellite/observation commits trust the durable record of
        # successful uploads and avoid a paginated 10k-object bucket listing.
        # The half-hour archive worker still performs a full reconciliation
        # and expiry deletion, repairing any externally removed object.
        if fast:
            remote = {key: values[0] for key, values in known_objects.items()}
            remote_modified: dict[str, dt.datetime] = {}
        else:
            inventory = list_remote_inventory(client, config.bucket)
            remote = inventory.sizes
            remote_modified = inventory.modified_at
        if not fast:
            # The fast path deliberately trusts this index, so a periodic
            # authoritative listing must discard records for objects no longer
            # present in R2. This keeps size estimates accurate and prevents a
            # later fast catalog from assuming a missing archive object exists.
            absent = set(known_objects).difference(remote)
            if absent:
                state.forget(absent)
                known_objects = {
                    key: value
                    for key, value in known_objects.items()
                    if key in remote
                }
        desired_keys = {item.key for item in objects}
        retained_video_keys = (
            retained_local_video_keys(root, remote, desired_keys=desired_keys)
            if not fast
            else set()
        )
        expired = (
            sorted(
                {
                    key
                    for key in (
                        *expired_remote_keys(remote, now),
                        *expired_video_keys(
                            remote,
                            now,
                            desired_keys=desired_keys | retained_video_keys,
                            modified_at=remote_modified,
                        ),
                    )
                    if key not in desired_keys
                }
            )
            if sync_delete and not fast
            else []
        )
        sizes = size_guard(
            objects,
            catalog_bytes,
            remote,
            config,
            pending_delete=expired,
        )
        pending = [
            item
            for item in objects
            if (
                item.key not in remote
                or known_objects.get(item.key) != (item.size, item.mtime_ns)
            )
        ]
        if dry_run:
            result: dict[str, object] = {
                "status": "dry-run",
                "updatedAt": format_utc(now),
                "bucket": config.bucket,
                "objects": len(objects),
                "pending": len(pending),
                "expired": len(expired),
                "fast": fast,
                "wholeFrameOnly": whole_frame_only,
                "recoveryHours": recovery_hours,
                **sizes,
            }
            write_status(status_path, result)
            return result

        def upload(item: LocalObject) -> tuple[LocalObject, str]:
            return item, upload_object(client, config, item)

        uploaded = 0
        with ThreadPoolExecutor(
            max_workers=min(UPLOAD_WORKERS, max(1, len(pending)))
        ) as executor:
            futures = {executor.submit(upload, item): item for item in pending}
            for future in as_completed(futures):
                item, sha256 = future.result()
                # Keep SQLite writes on the publishing thread. Only the
                # independent network transfers run concurrently. Recording
                # futures as they finish also prevents one slow PUT from
                # hiding the successful progress of every later upload.
                state.record(item, sha256)
                uploaded += 1

        # Commit complete compatibility catalogs first. The small operational
        # index is last, so new clients cannot discover a generation until all
        # of its assets and its on-demand full fallback are public.
        upload_catalog(
            client,
            config,
            westwx_catalog_bytes,
            key="westwx-catalog.json",
        )
        upload_catalog(client, config, catalog_bytes)
        upload_catalog(
            client,
            config,
            catalog_index_bytes,
            key="catalog-index.json",
        )

        # Deletion is intentionally after the catalog commit and is limited to
        # objects whose timestamp independently violates the retention policy.
        deleted = delete_objects(client, config, expired) if expired else 0
        if expired:
            state.forget(expired)

        result = {
            "status": "ok",
            "updatedAt": format_utc(dt.datetime.now(UTC)),
            "bucket": config.bucket,
            "objects": len(objects),
            "uploaded": uploaded,
            "unchanged": len(objects) - uploaded,
            "deleted": deleted,
            "catalogLast": True,
            "fast": fast,
            "wholeFrameOnly": whole_frame_only,
            "recoveryHours": recovery_hours,
            **sizes,
        }
        if config.public_base_url:
            result["catalogUrl"] = f"{config.public_base_url}/catalog.json"
            result["catalogIndexUrl"] = (
                f"{config.public_base_url}/catalog-index.json"
            )
        write_status(status_path, result)
        return result
    finally:
        state.close()
        if snapshot_root is not None:
            shutil.rmtree(snapshot_root, ignore_errors=True)


def write_publish_error(status_path: Path, error: Exception) -> None:
    write_status(
        status_path,
        {
            "status": "error",
            "updatedAt": format_utc(dt.datetime.now(UTC)),
            "error": f"{type(error).__name__}: {error}",
        },
    )
