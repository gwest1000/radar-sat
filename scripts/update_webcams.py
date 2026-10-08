"""Refresh public camera metadata only (no image downloads).

Run from the repository root: .venv/bin/python -m scripts.update_webcams
The generated JSON ships with the static site; refresh before deploying.
"""
import json
from pathlib import Path

import requests

from radarsat.webcams import discover_webcams


def main():
    with requests.Session() as session:
        payload = discover_webcams(session)
    target = Path(__file__).resolve().parents[1] / "public" / "webcams.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    temporary.replace(target)
    print(f"Saved {len(payload['cameras'])} webcams; domain counts: "
          f"{ {key: len(value) for key, value in payload['domains'].items()} }")


if __name__ == "__main__":
    main()
