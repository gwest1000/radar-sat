from __future__ import annotations

import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts import check_eccc_feeds


PROJECT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT / "config" / "sarracenia" / "subscribe"


class EcccFeedConfigTests(unittest.TestCase):
    def test_repository_configs_have_required_bindings(self) -> None:
        self.assertEqual(check_eccc_feeds.check_configs(CONFIG_DIR), [])

    def test_accept_filters_keep_only_selected_products(self) -> None:
        samples = {
            "radarsat_goes_west.conf": (
                "https://dd.weather.gc.ca/20260720/WXO-DD/satellite/goes/west/23/"
                "20260720T2330Z_MSC_GOES-West_DayVis-NightIR_1km.tif"
            ),
            "radarsat_lightning.conf": (
                "https://dd.weather.gc.ca/20260720/WXO-DD/lightning/"
                "20260720T2330Z_MSC_Lightning_2.5km.tif"
            ),
            "radarsat_bc_site_radar.conf": (
                "https://dd.weather.gc.ca/20260720/WXO-DD/radar/DPQPE/GIF/CASAG/"
                "20260720T2330Z_MSC_Radar-DPQPE_CASAG_Rain-Contingency.gif"
            ),
        }
        for config_name, sample in samples.items():
            settings = check_eccc_feeds.parse_config(CONFIG_DIR / config_name)
            self.assertTrue(any(re.search(pattern, sample) for pattern in settings["accept"]))

        satellite = check_eccc_feeds.parse_config(CONFIG_DIR / "radarsat_goes_west.conf")
        unselected = (
            "https://dd.weather.gc.ca/20260720/WXO-DD/satellite/goes/west/23/"
            "20260720T2330Z_MSC_GOES-West_Ash_2km.tif"
        )
        self.assertFalse(any(re.search(pattern, unselected) for pattern in satellite["accept"]))

    def test_live_spool_check_uses_source_time_and_magic(self) -> None:
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y%m%dT%H%MZ")
        compact = now.strftime("%Y%m%d%H%M")
        with tempfile.TemporaryDirectory() as temporary:
            spool = Path(temporary)
            tiffs = [
                spool / "satellite" / f"{timestamp}_MSC_GOES-West_DayVis-NightIR_1km.tif",
                spool / "satellite" / f"{timestamp}_MSC_GOES-West_NightIR_2km.tif",
                spool / "satellite" / (
                    f"{timestamp}_MSC_GOES-West_VisibleIRSandwich-NightMicrophysicsIR_1km.tif"
                ),
                spool / "satellite" / (
                    f"{timestamp}_MSC_GOES-West_SnowFog-NightMicrophysics_1km.tif"
                ),
                spool / "lightning" / f"{timestamp}_MSC_Lightning_2.5km.tif",
            ]
            gifs = [
                path
                for station in ("CASAG", "CASHP", "CASSS", "CASPG")
                for path in (
                    spool / "radar" / f"{timestamp}_MSC_Radar-DPQPE_{station}_Rain.gif",
                    spool / "radar" / f"{compact}_{station}_CAPPI_1.5_RAIN.gif",
                )
            ]
            for path in (*tiffs, *gifs):
                path.parent.mkdir(parents=True, exist_ok=True)
            for path in tiffs:
                path.write_bytes(b"II*\x00\x08\x00")
            for path in gifs:
                path.write_bytes(b"GIF89a")
            self.assertEqual(check_eccc_feeds.check_spool(spool, require_data=True), [])


if __name__ == "__main__":
    unittest.main()
