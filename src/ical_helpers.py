import re
from datetime import datetime, timedelta, time, timezone
from typing import Optional

from icalendar import vDatetime, vDate
from loguru import logger

from config import COURSE_DUE_TIMES, CURRENT_TZ, EVENT_LENGTH, RE_LINK_ASSIGN_OR_EVENT, RE_LINK_DISCUSSION, \
    MARK_DONE_BASE_URL
from manual_mark_helpers import occurrence_token_for_due_date


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


def set_due_time(ev, dt, hhmm: time):
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


def clean_description(ev, item_id=None, item_type=None, sdt=None, sid=None, assignment_submissions=None,
                      get_submission_status_func=None):
    desc = ev.get("DESCRIPTION", "")
    desc = re.sub(RE_LINK_ASSIGN_OR_EVENT, "", desc)
    desc = re.sub(RE_LINK_DISCUSSION, "", desc)

    if sdt:
        desc = sdt.strftime("📅 %a, %b %-d at %-I:%M %p") + "\n\n" + desc

    # Add appropriate action links for assignments and discussions
    if item_type in ["assignment", "discussion"] and item_id:
        # Check if item is already marked as done
        is_marked_done = False
        if assignment_submissions and get_submission_status_func and sdt and sid:
            submission_status = get_submission_status_func(item_id, sdt, sid, assignment_submissions, item_type)
            is_marked_done = (submission_status == "✅")

        occ_token = occurrence_token_for_due_date(sdt)

        if is_marked_done:
            # Show unmark link for items that are marked as done
            if occ_token:
                unmark_done_url = f"{MARK_DONE_BASE_URL}/unmark-done/{item_id}?occ={occ_token}"
            else:
                unmark_done_url = f"{MARK_DONE_BASE_URL}/unmark-done/{item_id}"
            action_link = f"\n\n↩️ Unmark as Done: {unmark_done_url}"
        else:
            # Show mark as done link for items that are not marked as done
            if occ_token:
                mark_done_url = f"{MARK_DONE_BASE_URL}/mark-done/{item_id}?occ={occ_token}"
            else:
                mark_done_url = f"{MARK_DONE_BASE_URL}/mark-done/{item_id}"
            action_link = f"\n\n📝 Mark as Done: {mark_done_url}"

        desc += action_link

    ev["DESCRIPTION"] = desc.replace("\n\n\n\n", "\n\n")


def add_status_symbol(ev, sdt, item_id, item_type, sid, ASSIGNMENT_SUBMISSIONS, get_submission_status_func):
    if item_type == "assignment":
        submission_status = get_submission_status_func(item_id, sdt, sid, ASSIGNMENT_SUBMISSIONS, item_type)
        ev["SUMMARY"] = f"{submission_status} {ev['SUMMARY']}"
    elif item_type == "discussion":
        # Check if discussion is manually marked as done
        submission_status = get_submission_status_func(item_id, sdt, sid, ASSIGNMENT_SUBMISSIONS, item_type)
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
