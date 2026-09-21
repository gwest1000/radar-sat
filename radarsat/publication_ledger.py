"""Shared, per-object serialized uploads for rapid and complete publishers."""
from __future__ import annotations
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import sqlite3
from pathlib import Path
from .publication_content import semantic_digest


@contextmanager
def object_lock(state_path: Path, key: str):
    directory = state_path.parent / 'upload-locks'
    directory.mkdir(parents=True, exist_ok=True)
    slot = int(hashlib.sha256(key.encode()).hexdigest()[:4], 16) % 256
    with (directory / f'{slot:03}.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def put_if_changed(client, config, item, state_path: Path, *, pointer: bool = False):
    from .r2 import retry, content_type, cache_control, CATALOG_CACHE_CONTROL
    body = item.path.read_bytes()  # Freeze exactly the bytes hashed and uploaded.
    digest = hashlib.sha256(body).hexdigest()
    comparison = semantic_digest(body) if pointer else digest
    generated = str(json.loads(body).get('generatedAt', '')) if pointer else ''
    with object_lock(state_path, item.key), sqlite3.connect(state_path, timeout=30) as db:
        db.execute('CREATE TABLE IF NOT EXISTS pointer_content (key TEXT PRIMARY KEY, digest TEXT, generated TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS upload_counts (day TEXT, prefix TEXT, puts INTEGER, reused INTEGER, PRIMARY KEY(day,prefix))')
        scope = db.execute("SELECT value FROM metadata WHERE key='scope'").fetchone()
        if scope is None or scope[0] != f'{config.account_id}/{config.bucket}':
            raise RuntimeError('Upload ledger scope mismatch')
        known = db.execute('SELECT size_bytes,sha256 FROM objects WHERE object_key=?', (item.key,)).fetchone()
        prior = db.execute('SELECT digest,generated FROM pointer_content WHERE key=?', (item.key,)).fetchone() if pointer else None
        unchanged = (known is not None and known[1] == digest) or (pointer and known is not None and prior is not None and (prior[0] == comparison or generated < prior[1]))
        now = dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
        if not unchanged:
            retry(lambda: client.put_object(Bucket=config.bucket, Key=item.key, Body=body,
                  ContentType='application/json; charset=utf-8' if pointer else content_type(item.path),
                  CacheControl=CATALOG_CACHE_CONTROL if pointer else cache_control(item.key),
                  Metadata={'sha256': digest}), f'Upload {item.key}')
            db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?,?,?)', (item.key, len(body), item.mtime_ns, digest, now))
            if pointer:
                db.execute('INSERT OR REPLACE INTO pointer_content VALUES (?,?,?)', (item.key, comparison, generated))
        elif pointer and (prior is None or generated > prior[1]):
            db.execute('INSERT OR REPLACE INTO pointer_content VALUES (?,?,?)', (item.key, comparison, generated))
        elif not pointer:
            db.execute('UPDATE objects SET size_bytes=?,mtime_ns=? WHERE object_key=?', (len(body),item.mtime_ns,item.key))
        prefix = item.key.split('/')[0]
        db.execute('INSERT INTO upload_counts VALUES (?,?,?,?) ON CONFLICT(day,prefix) DO UPDATE SET puts=puts+excluded.puts,reused=reused+excluded.reused',
                   (now[:10],prefix,int(not unchanged),int(unchanged)))
    return digest, not unchanged


def put_pointer(client, config, payload: bytes, key: str, state_path: Path) -> bool:
    from .r2 import LocalObject
    import tempfile
    with tempfile.NamedTemporaryFile(dir=state_path.parent) as handle:
        handle.write(payload)
        handle.flush()
        path = Path(handle.name)
        item = LocalObject(key, path, len(payload), path.stat().st_mtime_ns)
        return put_if_changed(client, config, item, state_path, pointer=True)[1]
