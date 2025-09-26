from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from config import CURRENT_TZ


def occurrence_token_for_due_date(due: datetime | date | None) -> Optional[str]:
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

    return local_dt.strftime("%Y%m%dT%H%M%S")


def manual_mark_key(item_id: str, occurrence_token: str | None) -> str:
    """Build the storage key used for manual marks."""
    item = str(item_id)
    if occurrence_token:
        return f"{item}::{occurrence_token}"
    return item
