from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from radarsat.live_edge import build_live_edge_index, publish_live_edge
from radarsat.r2 import R2Config


UTC = dt.timezone.utc


class FakeR2:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def put_object(self, **kwargs: object) -> dict[str, object]:
        self.keys.append(str(kwargs["Key"]))
        body = kwargs.get("Body")
        if hasattr(body, "read"):
            body.read()
        return {}


class LiveEdgeIndexTests(unittest.TestCase):
    def test_regional_south_coast_radar_is_included(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frame = root / "frames/bc/radar-rain-region-south-coast/2026/08/14/20260814T1906Z.png"
            frame.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (4, 3), (0, 0, 0, 0)).save(frame)
            metadata = root / "metadata/bc/radar-rain-region-south-coast/2026/08/14/20260814T1906Z.json"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text(json.dumps({
                "validTime": "2026-08-14T19:06:00Z",
                "path": frame.relative_to(root).as_posix(),
                "source": "ECCC GeoMet + NOAA NEXRAD Level III",
                "sourceLayer": "radar",
                "fetchedAt": "2026-08-14T19:07:00Z",
            }))

            payload, objects = build_live_edge_index(root)

            layer = payload["domains"]["bc"]["layers"]["radar-rain-region-south-coast"]
            self.assertEqual(layer["frames"][0]["validTime"], "2026-08-14T19:06:00Z")
            self.assertEqual([item.key for item in objects], [frame.relative_to(root).as_posix()])

    def test_build_selects_latest_complete_frame_per_hot_layer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for minute in (0, 6):
                stamp = f"20260814T19{minute:02d}Z"
                frame = root / f"frames/bc/radar-rain/2026/08/14/{stamp}.png"
                frame.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (4, 3), (0, 0, 0, 0)).save(frame)
                metadata = root / f"metadata/bc/radar-rain/2026/08/14/{stamp}.json"
                metadata.parent.mkdir(parents=True, exist_ok=True)
                metadata.write_text(json.dumps({
                    "validTime": f"2026-08-14T19:{minute:02d}:00Z",
                    "path": frame.relative_to(root).as_posix(),
                    "source": "ECCC GeoMet",
                    "sourceLayer": "radar",
                    "fetchedAt": "2026-08-14T19:07:00Z",
                }))

            payload, objects = build_live_edge_index(
                root,
                now=dt.datetime(2026, 8, 14, 19, 8, tzinfo=UTC),
            )

            frames = payload["domains"]["bc"]["layers"]["radar-rain"]["frames"]
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0]["validTime"], "2026-08-14T19:06:00Z")
            self.assertEqual(len(objects), 1)
            self.assertEqual(objects[0].key, frames[0]["path"])

    def test_publish_uploads_only_changed_rasters_and_commits_index_last(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = dt.datetime(2026, 8, 14, 19, 6, tzinfo=UTC)
            frame = root / "frames/bc/radar-rain/2026/08/14/20260814T1906Z.png"
            frame.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGBA", (4, 3), (0, 0, 0, 0)).save(frame)
            metadata = root / "metadata/bc/radar-rain/2026/08/14/20260814T1906Z.json"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_text(json.dumps({
                "validTime": "2026-08-14T19:06:00Z",
                "path": frame.relative_to(root).as_posix(),
                "source": "ECCC GeoMet",
                "sourceLayer": "radar",
                "fetchedAt": "2026-08-14T19:07:00Z",
            }))
            state = root / "state/live-edge.json"
            client = FakeR2()
            config = R2Config("account", "access", "secret", bucket="radar-sat")

            first = publish_live_edge(root, config, client=client, now=valid, state_path=state)
            second = publish_live_edge(root, config, client=client, now=valid, state_path=state)

            self.assertEqual(first["uploadedObjects"], 1)
            self.assertEqual(second["uploadedObjects"], 0)
            self.assertEqual(client.keys, [frame.relative_to(root).as_posix(), "live-edge.json", "live-edge.json"])
            self.assertTrue((root / "live-edge.json").is_file())


if __name__ == "__main__":
    unittest.main()
