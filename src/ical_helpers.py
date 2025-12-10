from datetime import timezone, date
from typing import Optional

from icalendar import vDatetime, vDate, Event

from config import *
from manual_mark_helpers import get_occ_token


def course_due_time(course_title: str) -> Optional[time]:
    """Return HH:MM as a tz-aware time in local tz, based on substring match."""
    for key, tstr in COURSE_DUE_TIMES.items():
        if key.lower() in course_title.lower():
            hh, mm = map(int, tstr.split(":"))
            return time(hour=hh, minute=mm, tzinfo=CURRENT_TZ)
    return None


def as_all_day(ev, day):
    ev["DTSTART"] = vDate(day)
    ev["DTEND"] = vDate(day + timedelta(days=1))
    if "DURATION" in ev:
        del ev["DURATION"]


def set_due_time(ev: Event, dt: datetime | date, hhmm: time):
    """
    Set DTSTART/DTEND to the course-defined time on the event's *local date*,
    then emit in UTC. Default duration = 50 minutes.
    """

    def to_utc(local_dt: datetime) -> datetime:
        if local_dt.tzinfo is None:
            local_dt = local_dt.replace(tzinfo=CURRENT_TZ)
        return local_dt.astimezone(timezone.utc)

    if isinstance(dt, datetime):
        # Use the date in local tz
        local_date = dt.astimezone(CURRENT_TZ).date() if dt.tzinfo else dt.date()
        local_dt = datetime.combine(local_date, hhmm)  # hhmm has local tz
    else:
        # dt is date (all-day). Schedule on that local day.
        local_dt = datetime.combine(dt, hhmm)

    utc_start = to_utc(local_dt)
    utc_end = utc_start + EVENT_LENGTH

    ev["DTSTART"] = vDatetime(utc_start)
    ev["DTEND"] = vDatetime(utc_end)
    if "DURATION" in ev:
        del ev["DURATION"]


def clean_description(ev, item_id, item_type, sdt, sid, submission_status):
    desc = ev.get("DESCRIPTION", "")
    desc = re.sub(RE_LINK_ASSIGN_OR_EVENT, "", desc)
    desc = re.sub(RE_LINK_DISCUSSION, "", desc)

    if sdt:
        desc = sdt.strftime("📅 %a, %b %-d at %-I:%M %p") + "\n\n" + desc

    # Add appropriate action links for assignments and discussions
    if item_type in ["assignment", "discussion"] and item_id:
        # Check if item is already marked as done
        is_marked_done = False
        if sdt and sid:
            is_marked_done = (submission_status == "✅")

        occ_token = get_occ_token(sdt)

        if is_marked_done:
            # Show unmark link for items that are marked as done
            if occ_token:
                unmark_done_url = f"{BASE_URL}/api/unmark-done/{item_id}?occ={occ_token}"
            else:
                unmark_done_url = f"{BASE_URL}/api/unmark-done/{item_id}"
            action_link = f"\n\n↩️ Unmark as Done: {unmark_done_url}"
        else:
            # Show mark as done link for items that are not marked as done
            if occ_token:
                mark_done_url = f"{BASE_URL}/api/mark-done/{item_id}?occ={occ_token}"
            else:
                mark_done_url = f"{BASE_URL}/api/mark-done/{item_id}"
            action_link = f"\n\n📝 Mark as Done: {mark_done_url}"

        desc += action_link

    ev["DESCRIPTION"] = desc.replace("\n\n\n\n", "\n\n")


def add_status_symbol(ev, item_type, submission_status):
    if item_type == "assignment":
        ev["SUMMARY"] = f"{submission_status} {ev['SUMMARY']}"
    elif item_type == "discussion":
        # Check if discussion is manually marked as done
        if submission_status == "✅":
            ev["SUMMARY"] = f"✅ {ev['SUMMARY']}"
        else:
            ev["SUMMARY"] = f"💬 {ev['SUMMARY']}"
    elif item_type == "assessment":
        ev["SUMMARY"] = f"🧪 {ev['SUMMARY']}"
    elif item_type == "event":
        ev["SUMMARY"] = f"🗓 {ev['SUMMARY']}️"
    else:
        ev["SUMMARY"] = f"🤷 {ev['SUMMARY']}"
