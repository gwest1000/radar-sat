from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from radarsat.images import save_coverage


class CoverageRenderTests(unittest.TestCase):
    def test_inverted_source_hatches_no_coverage_not_valid_footprint(self) -> None:
        source = np.zeros((4, 6, 4), dtype=np.uint8)
        source[:, :3] = (181, 181, 181, 128)  # GeoMet no-coverage paint
        encoded = io.BytesIO()
        Image.fromarray(source).save(encoded, "PNG")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "coverage.png"
            save_coverage(encoded.getvalue(), destination)
            alpha = np.asarray(Image.open(destination).convert("RGBA"))[:, :, 3]
        self.assertGreater(int(alpha[:, :3].max()), 0)
        self.assertEqual(int(alpha[:, 3:].max()), 0)


if __name__ == "__main__":
    unittest.main()
