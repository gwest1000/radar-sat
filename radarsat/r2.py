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
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import DOMAINS
from .geomet import format_utc
from .pipeline import write_status
from .retention import keep_frame


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
DEFAULT_WARN_BYTES = 4_000_000_000
DEFAULT_MAX_BYTES = 5_000_000_000
STAMP_RE = re.compile(r"^(\d{8}T\d{4}Z)$")


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_local_path(root: Path, relative: str) -> Path:
    if not relative or relative.startswith("/"):
        raise PublicationSafetyError(f"Unsafe catalog path: {relative!r}")
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise PublicationSafetyError(f"Catalog path escapes output root: {relative!r}")
    if not candidate.is_file() or candidate.stat().st_size <= 0:
        raise PublicationSafetyError(f"Catalog asset is missing or empty: {relative}")
    return candidate


def _metadata_path_for_frame(root: Path, frame_key: str) -> Path:
    parts = Path(frame_key).parts
    if not parts or parts[0] != "frames":
        raise PublicationSafetyError(f"Unexpected frame path: {frame_key}")
    return root.joinpath("metadata", *parts[1:]).with_suffix(".json")


def discover_objects(root: Path) -> tuple[list[LocalObject], bytes]:
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

    relative_paths: set[str] = set()
    frame_count = 0
    for domain in catalog.get("domains", {}).values():
        for layer in domain.get("layers", {}).values():
            for frame in layer.get("frames", []):
                key = str(frame.get("path", ""))
                _safe_local_path(root, key)
                relative_paths.add(key)
                metadata_path = _metadata_path_for_frame(root, key)
                if not metadata_path.is_file():
                    raise PublicationSafetyError(f"Missing frame metadata: {metadata_path}")
                relative_paths.add(metadata_path.relative_to(root).as_posix())
                frame_count += 1
        for static in domain.get("staticLayers", {}).values():
            relative_paths.add(str(static.get("path", "")))
    for legend in catalog.get("legends", {}).values():
        if legend.get("path"):
            relative_paths.add(str(legend["path"]))

    if frame_count == 0:
        raise PublicationSafetyError("Refusing to publish a catalog containing zero frames")

    objects: list[LocalObject] = []
    for key in sorted(relative_paths):
        path = _safe_local_path(root, key)
        stat = path.stat()
        objects.append(LocalObject(key=key, path=path, size=stat.st_size, mtime_ns=stat.st_mtime_ns))
    return objects, catalog_bytes


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
            max_pool_connections=4,
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


def list_remote_objects(client: Any, bucket: str) -> dict[str, int]:
    objects: dict[str, int] = {}
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
            objects[str(item["Key"])] = int(item.get("Size", 0))
        if not response.get("IsTruncated"):
            break
        token = str(response.get("NextContinuationToken", ""))
        if not token:
            raise RuntimeError("R2 returned a truncated listing without a continuation token")
    return objects


def content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed == "application/json":
        return "application/json; charset=utf-8"
    return guessed or "application/octet-stream"


def cache_control(key: str) -> str:
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


def upload_catalog(client: Any, config: R2Config, payload: bytes) -> None:
    retry(
        lambda: client.put_object(
            Bucket=config.bucket,
            Key="catalog.json",
            Body=payload,
            ContentType="application/json; charset=utf-8",
            CacheControl=CATALOG_CACHE_CONTROL,
        ),
        "Upload catalog.json",
    )


def remote_valid_time(key: str) -> tuple[dt.datetime, str] | None:
    parts = Path(key).parts
    if len(parts) < 7 or parts[0] not in {"frames", "metadata"}:
        return None
    domain_id = parts[1]
    stamp = Path(parts[-1]).stem
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
        if not keep_frame(valid_time, now, tier):
            expired.append(key)
    return sorted(expired)


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
) -> dict[str, int]:
    values = list(objects)
    local_bytes = sum(item.size for item in values) + len(catalog_bytes)
    remote_bytes = sum(remote.values())
    replaced_bytes = sum(remote.get(item.key, 0) for item in values) + remote.get(
        "catalog.json", 0
    )
    projected_bytes = remote_bytes - replaced_bytes + local_bytes
    if local_bytes >= config.warn_bytes or projected_bytes >= config.warn_bytes:
        print(
            "R2 storage warning: "
            f"retained={local_bytes / 1_000_000_000:.2f} GB, "
            f"projected bucket={projected_bytes / 1_000_000_000:.2f} GB",
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
    }


def publish(
    root: Path,
    config: R2Config,
    state_path: Path,
    status_path: Path,
    *,
    client: Any | None = None,
    sync_delete: bool = True,
    dry_run: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    now = (now or dt.datetime.now(UTC)).astimezone(UTC)
    objects, catalog_bytes = discover_objects(root)
    client = client or boto3_client(config)
    remote = list_remote_objects(client, config.bucket)
    sizes = size_guard(objects, catalog_bytes, remote, config)
    state = PublishState(state_path, f"{config.account_id}/{config.bucket}")
    try:
        pending = [
            item
            for item in objects
            if item.key not in remote or not state.unchanged(item)
        ]
        desired_keys = {item.key for item in objects}
        expired = (
            [
                key
                for key in expired_remote_keys(remote, now)
                if key not in desired_keys
            ]
            if sync_delete
            else []
        )
        if dry_run:
            result: dict[str, object] = {
                "status": "dry-run",
                "updatedAt": format_utc(now),
                "bucket": config.bucket,
                "objects": len(objects),
                "pending": len(pending),
                "expired": len(expired),
                **sizes,
            }
            write_status(status_path, result)
            return result

        uploaded = 0
        for item in pending:
            sha256 = upload_object(client, config, item)
            state.record(item, sha256)
            uploaded += 1

        # This is the commit point: browsers cannot observe references to an
        # object until every referenced asset has uploaded successfully.
        upload_catalog(client, config, catalog_bytes)

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
            **sizes,
        }
        if config.public_base_url:
            result["catalogUrl"] = f"{config.public_base_url}/catalog.json"
        write_status(status_path, result)
        return result
    finally:
        state.close()


def write_publish_error(status_path: Path, error: Exception) -> None:
    write_status(
        status_path,
        {
            "status": "error",
            "updatedAt": format_utc(dt.datetime.now(UTC)),
            "error": f"{type(error).__name__}: {error}",
        },
    )
