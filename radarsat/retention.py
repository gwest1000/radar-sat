from __future__ import annotations

import datetime as dt


ECMWF_CONTOUR_LAYERS = frozenset({"ecmwf-hgt500", "ecmwf-mslp"})
ECMWF_HOURLY_RETENTION_HOURS = 24
ECMWF_SOURCE_INTERVAL_HOURS = 3
RAPID_RETENTION_HOURS = 3


def keep_frame(valid_time: dt.datetime, now: dt.datetime, tier: str) -> bool:
    age = now - valid_time
    if age < dt.timedelta(0):
        return True
    if age > dt.timedelta(days=7):
        return False
    if age <= dt.timedelta(hours=24):
        return True
    return valid_time.minute == 0 and valid_time.hour % 3 == 0


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
    if layer_id.startswith("glm-lightning-live"):
        return age <= dt.timedelta(minutes=30)
    if layer_id in {"raw-visir-native", "raw-visir-5min"}:
        if age <= dt.timedelta(hours=RAPID_RETENTION_HOURS):
            return True
        return age <= dt.timedelta(hours=24) and valid_time.minute % 10 == 0
    if layer_id.startswith("radar-rain"):
        if age <= dt.timedelta(hours=RAPID_RETENTION_HOURS):
            return True
        if age <= dt.timedelta(hours=24):
            # ECCC's native composite is six-minute data. Twelve-minute
            # thinning preserves every other scan without inventing a
            # nominal ten-minute observation time.
            return valid_time.second == 0 and valid_time.minute % 12 == 0
    if (
        layer_id in ECMWF_CONTOUR_LAYERS
        and age > dt.timedelta(hours=ECMWF_HOURLY_RETENTION_HOURS)
        and valid_time.hour % ECMWF_SOURCE_INTERVAL_HOURS != 0
    ):
        return False
    return True
