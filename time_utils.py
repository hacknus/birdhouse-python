from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo


BERN_TIMEZONE = ZoneInfo("Europe/Zurich")


def bern_image_timestamp() -> str:
    """Return an image timestamp formatted in Bern local time."""
    return datetime.datetime.now(BERN_TIMEZONE).strftime("%Y%m%d_%H%M%S")
