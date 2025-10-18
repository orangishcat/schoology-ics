from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from config import CURRENT_TZ


def get_occ_token(due: datetime | date | None) -> Optional[str]:
    """Return a stable token that identifies an item occurrence on a specific local date/time."""
    if due is None:
        return None

    if isinstance(due, datetime):
        dt = due
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CURRENT_TZ)
        local_dt = dt.astimezone(CURRENT_TZ)
    elif isinstance(due, date):
        local_dt = datetime.combine(due, time.min, tzinfo=CURRENT_TZ)
    else:
        return None

    return local_dt.strftime("%Y%m%dT%H%M")


def normalize_occurrence_token(token: str | None) -> Optional[str]:
    """Normalize an occurrence token to minute precision."""
    if not token:
        return None
    token_str = str(token)
    return token_str[:13]
