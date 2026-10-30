"""Build the opt-in storm-track trial. Existing output survives failed downloads.

Run: .venv/bin/python -m scripts.update_storm_tracks
This only refreshes the small site JSON; it does not publish or rebuild videos.
"""
import argparse
import json
from pathlib import Path

from radarsat.storm_tracks import fetch_latest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("public/storm-tracks.json"))
    args = parser.parse_args()
    payload = fetch_latest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    temporary.replace(args.output)
    print(f"RDPS {payload['referenceTime']}: {len(payload['domains']['bc'])} BC-approach tracks; "
          f"{args.output.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
