#!/usr/bin/env python3
"""Bound raw ECCC spool growth; dry-run unless --apply is supplied."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


FEED_SUFFIXES = {"satellite": ".tif", "lightning": ".tif", "radar": ".gif"}


def preserved_names(status_path: Path | None) -> set[str]:
    if status_path is None:
        return set()
    try:
        payload = json.loads(status_path.read_text())
        domains = payload.get("spool", {}).get("domains", {})
        values = {
            str(name)
            for domain in domains.values()
            for name in domain.get("preserveFiles", [])
        }
    except (AttributeError, OSError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read ingest preservation state {status_path}: {error}") from error
    return {name for name in values if name and Path(name).name == name}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spool",
        type=Path,
        default=Path.home() / ".local" / "share" / "radar-sat" / "spool" / "eccc",
    )
    parser.add_argument("--older-than-hours", type=float, default=3.0)
    parser.add_argument(
        "--ingest-status",
        type=Path,
        help="ingest.json whose rejected source files must be preserved",
    )
    parser.add_argument("--apply", action="store_true", help="delete candidates instead of listing them")
    args = parser.parse_args()

    if args.older_than_hours < 1:
        parser.error("--older-than-hours must be at least 1")
    spool = args.spool.expanduser().resolve()
    if spool in (Path("/"), Path.home().resolve()):
        parser.error("refusing to operate on a broad filesystem path")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.older_than_hours)
    try:
        preserve = preserved_names(args.ingest_status)
    except RuntimeError as error:
        parser.error(str(error))
    candidates: list[Path] = []
    retained_bytes = 0
    total_bytes = 0
    for feed, suffix in FEED_SUFFIXES.items():
        directory = spool / feed
        paths = sorted(
            (
                path
                for path in directory.rglob(f"*{suffix}")
                if path.is_file()
                and not path.is_symlink()
                and not any(part.startswith(".") for part in path.relative_to(directory).parts)
                and path.resolve().is_relative_to(directory.resolve())
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ) if directory.is_dir() else []
        newest = paths[0] if paths else None
        for path in paths:
            stat = path.stat()
            total_bytes += stat.st_size
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if path != newest and modified < cutoff and path.name not in preserve:
                candidates.append(path)
            else:
                retained_bytes += stat.st_size

    candidate_bytes = total_bytes - retained_bytes
    verb = "DELETE" if args.apply else "WOULD DELETE"
    for path in candidates:
        print(f"{verb} {path}")
        if args.apply:
            path.unlink()
    print(
        f"mode={'apply' if args.apply else 'dry-run'} candidates={len(candidates)} "
        f"candidate_bytes={candidate_bytes} retained_bytes={retained_bytes} "
        f"preserved={len(preserve)} spool={spool}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
