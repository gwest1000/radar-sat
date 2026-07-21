from __future__ import annotations

import datetime as dt


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
