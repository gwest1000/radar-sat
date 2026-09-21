"""Lossless publication compaction; observation records keep their own clocks."""
from __future__ import annotations
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path


def enabled() -> bool:
    return os.environ.get('RADARSAT_R2_COMPACT_PUBLICATION', '0') == '1'


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        f.write(data)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def semantic_digest(payload: bytes) -> str:
    value = json.loads(payload)
    if isinstance(value, dict):
        value.pop('generatedAt', None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def project_frames(root: Path, catalog: dict, *, bundles: bool = True) -> set[str]:
    """Alias equal image bytes and retain original metadata in hourly bundles.

    Local source files/catalogs are untouched. Revisions and corrections change
    the content hash, so old browsers can finish while new ones get new pixels.
    """
    keys: set[str] = set()
    grouped: dict[str, dict] = {}
    db = sqlite3.connect(root / '.publication-hashes.sqlite3', timeout=30)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS hashes (path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, sha TEXT)')
    hashes = {row[0]: row[1:] for row in db.execute('SELECT path,size,mtime,sha FROM hashes')}
    updates = []
    db.execute('CREATE TABLE IF NOT EXISTS metadata_cache (path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, payload TEXT)')
    metadata_cache = {row[0]: row[1:] for row in db.execute('SELECT path,size,mtime,payload FROM metadata_cache')} if bundles else {}
    metadata_updates = []
    try:
        for domain_id, domain in catalog.get('domains', {}).items():
            for layer_id, layer in domain.get('layers', {}).items():
                for frame in layer.get('frames', []):
                    original = frame.get('originalPath', frame['path'])
                    relative = Path(original)
                    source = (root / relative).resolve()
                    if relative.is_absolute() or '..' in relative.parts or not source.is_relative_to(root.resolve()):
                        raise ValueError('Unsafe source path in publication')
                    stat = source.stat()
                    row = hashes.get(original)
                    sha = row[2] if row and row[:2] == (stat.st_size, stat.st_mtime_ns) else None
                    body = None
                    if sha is None:
                        body = source.read_bytes()
                        if source.stat().st_mtime_ns != stat.st_mtime_ns:
                            raise FileNotFoundError('Source changed during publication; retry snapshot')
                        sha = hashlib.sha256(body).hexdigest()
                        updates.append((original, stat.st_size, stat.st_mtime_ns, sha))
                    blob = f'frame-blobs/{domain_id}/{sha}{relative.suffix}'
                    destination = root / blob
                    if not destination.exists():
                        body = source.read_bytes() if body is None else body
                        if hashlib.sha256(body).hexdigest() != sha:
                            raise FileNotFoundError('Source corrected during publication; retry snapshot')
                        atomic_bytes(destination, body)
                    if bundles:
                        meta = Path('metadata', *relative.parts[1:]).with_suffix('.json').as_posix()
                        stamp = frame['validTime'][:13].replace('-', '').replace(':', '') + '00Z'
                        key = f'metadata-bundles/{domain_id}/{stamp}.json.gz'
                        # The full per-frame record is already read/validated by
                        # catalog construction. It includes all recovery fields.
                        meta_stat = (root / meta).stat()
                        cached = metadata_cache.get(meta)
                        if cached and cached[:2] == (meta_stat.st_size, meta_stat.st_mtime_ns):
                            metadata = json.loads(cached[2])
                        else:
                            raw = (root / meta).read_text()
                            metadata = json.loads(raw)
                            metadata_updates.append((meta, meta_stat.st_size, meta_stat.st_mtime_ns, raw))
                        grouped.setdefault(key, {})[meta] = {'metadata': metadata, 'blobPath': blob}
                    frame['originalPath'] = original
                    frame['path'] = blob
                    frame['contentSha256'] = sha
                    keys.add(blob)
        db.executemany('INSERT OR REPLACE INTO hashes VALUES (?,?,?,?)', updates)
        db.executemany('INSERT OR REPLACE INTO metadata_cache VALUES (?,?,?,?)', metadata_updates)
        db.commit()
    finally:
        db.close()
    for key, records in grouped.items():
        payload = {'schemaVersion': 1, 'records': records}
        data = gzip.compress(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode(), mtime=0)
        atomic_bytes(root / key, data)
        keys.add(key)
    if bundles:
        catalog['metadataRecovery'] = {'schemaVersion': 1, 'bundles': sorted(grouped)}
    return keys


def expired_compact_keys(remote, modified_at, desired_keys, now):
    """Protect current references; leave two hours for old browser catalogs."""
    import datetime as dt
    cutoff = now - dt.timedelta(hours=2)
    expired = [key for key in remote if key.startswith(('frame-blobs/', 'metadata-bundles/'))
               and key not in desired_keys and modified_at.get(key, now) < cutoff]
    if enabled():
        # After migration, keep old image URLs for a full day for open clients.
        legacy_cutoff = now - dt.timedelta(hours=26)
        expired.extend(key for key in remote if key.startswith(('frames/', 'metadata/'))
                       and key not in desired_keys and modified_at.get(key, now) < legacy_cutoff)
    return expired


def prune_local(root: Path, desired_keys: set[str], now) -> int:
    # Remote snapshots have their own copies. A two-day grace also protects
    # another publisher still preparing its immutable dependencies.
    cutoff = now.timestamp() - 48 * 3600
    removed = 0
    for prefix in ('frame-blobs', 'metadata-bundles'):
        for path in (root / prefix).glob('*/*'):
            if path.is_file() and path.relative_to(root).as_posix() not in desired_keys and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
    with sqlite3.connect(root / '.publication-hashes.sqlite3', timeout=30) as db:
        for table in ('hashes', 'metadata_cache'):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                db.execute(f'DELETE FROM {table} WHERE mtime < ?', (int((now.timestamp()-8*86400)*1e9),))
    return removed
