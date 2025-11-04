import json
from bisect import bisect_left
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from typing import List, Dict, Any, Optional, Union

from icalendar import Event, vDatetime

from config import USER_DATA_FILE, CURRENT_TZ, EVENT_LENGTH, get_stack_events, REPEAT_DAYS, BASE_URL
from ical_helpers import course_due_time, set_due_time, clean_description, add_status_symbol
from schoology_api_helpers import get_submission_status

repeat_to_timedelta = {
    "daily": timedelta(days=1),
    "weekly": timedelta(weeks=1),
    "monthly": timedelta(days=30),
    "yearly": timedelta(days=365)
}


def date_key(e):
    event_date = datetime.strptime(e.get("date"), "%Y-%m-%d")
    event_time = datetime.strptime(time if (time := e.get("time")) else "23:59", "%H:%M")
    event_dt = event_date.combine(event_date, event_time.time()).replace(tzinfo=CURRENT_TZ)
    return event_dt


last_cached = None

@lru_cache(maxsize=1)
def load_custom_events() -> List[Dict[str, Any]]:
    global last_cached
    date_now = last_cached = datetime.now(tz=CURRENT_TZ)

    if not USER_DATA_FILE.exists():
        return []

    cached = json.loads(USER_DATA_FILE.read_text())
    evs = cached.get("custom_events", [])

    if not isinstance(evs, list):
        return []

    for e in evs:
        event_date = datetime.strptime(e["date"], "%Y-%m-%d").date()
        if repeat_time := repeat_to_timedelta.get(e.get("repeat", "none")):
            delta = (date_now.date() - event_date).days
            next_repeat = event_date + (delta // repeat_time.days + 1) * repeat_time
            e["date"] = next_repeat.strftime("%Y-%m-%d")

    sorted_evs = sorted(evs, key=date_key)
    idx = bisect_left(sorted_evs, date_now, key=date_key)
    smaller, larger = sorted_evs[:idx], sorted_evs[idx:]
    smaller.reverse()
    return larger + smaller


def save_custom_events(events: List[Dict[str, Any]]):
    try:
        cached = {}
        if USER_DATA_FILE.exists():
            try:
                cached = json.loads(USER_DATA_FILE.read_text())
            except Exception:
                cached = {}
        cached["custom_events"] = events
        USER_DATA_FILE.write_text(json.dumps(cached, indent=2))
    except Exception:
        pass


def _parse_local_dt(date_str: str, time_str: Optional[str]) -> Optional[datetime]:
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        if time_str:
            t = datetime.strptime(time_str, "%H:%M").time()
            return datetime.combine(d, t).replace(tzinfo=CURRENT_TZ)
        else:
            # No time provided; return midnight local for now
            return datetime.combine(d, time(0, 0, tzinfo=CURRENT_TZ))
    except Exception:
        return None


def add_custom(cev: Dict[str, Any], assignment_stack_times) -> Union[Event, List[Event], None]:
    """Construct one or more icalendar Event(s) from a stored custom event dict.

    When repeating, expands occurrences within a forward horizon and stacks per date.
    """
    name = (cev.get("name") or "").strip() or "Custom Item"
    description = (cev.get("description") or "").strip()
    course_name = (cev.get("course_name") or "").strip()
    item_type = (cev.get("type") or "event").strip().lower()
    date_str = (cev.get("date") or "").strip()
    time_str = (cev.get("time") or "").strip()
    repeat = (cev.get("repeat") or "none").strip().lower()

    if not date_str:
        return None

    # Base identifiers used for decoration
    item_id = str(cev.get("id") or "")
    sid = "custom"

    ev = Event()
    ev.add('summary', name)
    if course_name:
        ev.add('location', course_name)
    ev.add('description', description + f"\n\nEdit: {BASE_URL}/custom/edit/{item_id}")

    # Determine start time handling.
    # When stacking is enabled, always stack regardless of an explicit time.
    local_dt = _parse_local_dt(date_str, (time_str or None))
    if local_dt is None:
        return None

    def _apply_time_for_date(target_ev: Event, local_dt_for_day: datetime):
        if get_stack_events():
            key = local_dt_for_day.date()
            desired_local = assignment_stack_times[key].time()
            assignment_stack_times[key] += EVENT_LENGTH
            set_due_time(target_ev, local_dt_for_day, desired_local)
        else:
            if time_str:
                start_utc = local_dt_for_day.astimezone(timezone.utc)
                target_ev['DTSTART'] = vDatetime(start_utc)
                target_ev['DTEND'] = vDatetime(start_utc + EVENT_LENGTH)
            else:
                desired = course_due_time(course_name) if course_name else None
                if desired:
                    set_due_time(target_ev, local_dt_for_day, desired)
                else:
                    noon = time(12, 0, tzinfo=CURRENT_TZ)
                    set_due_time(target_ev, local_dt_for_day, noon)

    def _clone_base() -> Event:
        ne = Event()
        ne.add('summary', name)
        if course_name:
            ne.add('location', course_name)
        if description:
            ne.add('description', description)
        return ne

    def _expand_dates(start_dt: datetime) -> List[datetime]:
        dates: List[datetime] = []
        cur = start_dt
        now_local = datetime.now(tz=CURRENT_TZ)
        end_by = now_local + timedelta(days=REPEAT_DAYS)

        def add_months(d: datetime, months: int) -> datetime:
            y = d.year + (d.month - 1 + months) // 12
            m = (d.month - 1 + months) % 12 + 1
            day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28,
                              31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
            return d.replace(year=y, month=m, day=day)

        if repeat == 'none' or not repeat:
            return [start_dt]
        while cur <= end_by:
            dates.append(cur)
            if repeat == 'daily':
                cur = cur + timedelta(days=1)
            elif repeat == 'weekly':
                cur = cur + timedelta(weeks=1)
            elif repeat == 'monthly':
                cur = add_months(cur, 1)
            elif repeat == 'yearly':
                try:
                    cur = cur.replace(year=cur.year + 1)
                except Exception:
                    # Handle Feb 29 by moving to Feb 28
                    cur = cur.replace(month=2, day=28, year=cur.year + 1)
            else:
                # Unknown repeat → single
                break
        return dates

    # Build one or many events
    if repeat and repeat != 'none':
        events: List[Event] = []
        for occ_dt in _expand_dates(local_dt):
            ne = _clone_base()
            # Apply timing
            _apply_time_for_date(ne, occ_dt)

            # Decorations
            if item_type == "assignment":
                sub_status = get_submission_status(ne, item_id, occ_dt, sid, item_type)
                clean_description(ne, item_id, "assignment", occ_dt, sid, sub_status)
                add_status_symbol(ne, "assignment", sub_status)
            else:
                ne['SUMMARY'] = f"🗓 {ne['SUMMARY']}"
            events.append(ne)
        return events
    else:
        _apply_time_for_date(ev, local_dt)

    # Add Schoology-like decorations for assignments
    sdt = ev.get('DTSTART')
    try:
        sdt_val = sdt.dt.astimezone(CURRENT_TZ) if hasattr(sdt, 'dt') else None
    except Exception:
        sdt_val = None

    if item_type == "assignment":
        sub_status = get_submission_status(ev, item_id, sdt, sdt_val, item_type)
        clean_description(ev, item_id, "assignment", sdt_val, sid, sub_status)
        add_status_symbol(ev, "assignment", sub_status)
    else:
        # Label events visually
        ev['SUMMARY'] = f"🗓 {ev['SUMMARY']}"

    # Apply simple recurrence rules if requested
    if repeat in {"daily", "weekly", "monthly", "yearly"}:
        freq = repeat.upper()
        try:
            ev.add('rrule', {"FREQ": [freq]})
        except Exception:
            # Fallback plain text RRULE
            ev.add('rrule', f"FREQ={freq}")

    return ev
