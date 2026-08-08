from __future__ import annotations

import datetime as dt


ECMWF_CONTOUR_LAYERS = frozenset({"ecmwf-hgt500", "ecmwf-mslp"})
ECMWF_HOURLY_RETENTION_HOURS = 24
ECMWF_SOURCE_INTERVAL_HOURS = 3


def keep_frame(valid_time: dt.datetime, now: dt.datetime, tier: str) -> bool:
    age = now - valid_time
    if age < dt.timedelta(0):
        return True
    if age > dt.timedelta(days=7):
        return False
    if age <= dt.timedelta(hours=24):
        return True
    if tier == "bc":
        return valid_time.minute in {0, 30}
    return valid_time.minute == 0


def keep_layer_frame(
    valid_time: dt.datetime,
    now: dt.datetime,
    tier: str,
    layer_id: str,
) -> bool:
    """Apply the general archive policy plus layer-specific cadence thinning."""
    if not keep_frame(valid_time, now, tier):
        return False
    age = now - valid_time
    if (
        layer_id in ECMWF_CONTOUR_LAYERS
        and age > dt.timedelta(hours=ECMWF_HOURLY_RETENTION_HOURS)
        and valid_time.hour % ECMWF_SOURCE_INTERVAL_HOURS != 0
    ):
        return False
    return True
