import time as _time

import schoolopy
from flask import abort
from requests_oauthlib import OAuth1

from config import *
from manual_mark_helpers import manual_mark_key, occurrence_token_for_due_date


# ------------------ SCHOOL0GY API -------------
def get_schoology_client():
    """Auth’d schoolopy client."""
    if not (SCHO_CONSUMER_KEY and SCHO_CONSUMER_SECRET):
        abort(500, "Set SCHOOLOGY_KEY and SCHOOLOGY_SECRET env vars.")
    return schoolopy.Schoology(schoolopy.Auth(SCHO_CONSUMER_KEY, SCHO_CONSUMER_SECRET))


def oauth():
    """OAuth1 session for Schoology requests."""
    if not (SCHO_CONSUMER_KEY and SCHO_CONSUMER_SECRET):
        abort(500, "Set SCHOOLOGY_KEY and SCHOOLOGY_SECRET env vars.")
    return OAuth1(SCHO_CONSUMER_KEY, SCHO_CONSUMER_SECRET)


def scho_get(path, params=None):
    """GET wrapper with OAuth and basic error handling."""
    try:
        r = requests.get(f"{SCHO_BASE}{path}", auth=oauth(), params=params, timeout=30)
    except Exception as e:
        if is_offline_error(e):
            ind = offline_indicator(e) or NO_WIFI_MSG
            logger.info(ind)
            abort(503, NO_WIFI_MSG)
        raise
    if r.status_code != 200:
        abort(502, f"Schoology API error {r.status_code} on {path}")
    return r.json()


def _cache_is_fresh(path: Path, max_age_secs: int) -> bool:
    if not path.exists():
        return False
    age = _time.time() - path.stat().st_mtime
    return age <= max_age_secs


def check_assignment_submission(assignment_id: str, section_id: str, user_id: str) -> dict:
    """
    Check if a user has submitted an assignment.
    Returns a dict:
      {
        "has_submission": bool,
        "submissions_disabled": bool,
        "error": Optional[str]
      }
    """
    try:
        r = requests.get(
            f"{SCHO_BASE}/sections/{section_id}/submissions/{assignment_id}/{user_id}",
            auth=oauth(),
            timeout=30
        )
        if r.status_code == 200:
            data = r.json()
            revisions = data.get("revision", [])
            submission_disabled = not data.get("allow_submissions", 1)
            has_submission = any(rev.get("draft", 1) == 0 for rev in revisions)
            return {
                "has_submission": has_submission,
                "submissions_disabled": submission_disabled
            }
        elif r.status_code == 404:
            # No submissions found → could mean either "not submitted yet" OR "submissions disabled"
            # Schoology usually 404s when submissions are disabled entirely
            return {
                "has_submission": False,
                "submissions_disabled": True
            }
        else:
            msg = f"Error checking submission for assignment {assignment_id}: {r.status_code}"
            logger.error(msg)
            return {
                "has_submission": False,
                "submissions_disabled": False,
                "error": msg
            }
    except Exception as e:
        if is_offline_error(e):
            ind = offline_indicator(e) or NO_WIFI_MSG
            logger.info(ind)
            return {
                "has_submission": False,
                "submissions_disabled": False,
                "error": NO_WIFI_MSG
            }
        msg = f"Exception checking submission for assignment {assignment_id}: {e}"
        logger.error(msg)
        return {
            "has_submission": False,
            "submissions_disabled": False,
            "error": str(e)
        }


def refresh_cache():
    """Force refresh and update in-memory maps."""
    global SECTION_ID_TO_NAME, ITEM_ID_TO_SECTION, ASSIGNMENT_SUBMISSIONS
    logger.info("Forcing cache refresh...")
    a, b, c = load_sections_and_items(force_refresh=True)
    SECTION_ID_TO_NAME.clear(); SECTION_ID_TO_NAME.update(a)
    ITEM_ID_TO_SECTION.clear(); ITEM_ID_TO_SECTION.update(b)
    ASSIGNMENT_SUBMISSIONS.clear(); ASSIGNMENT_SUBMISSIONS.update(c)


def _ymd(dt: datetime) -> str:
    """YYYY-MM-DD date string for Schoology params."""
    return dt.strftime("%Y-%m-%d")


def _fetch_user_events_window(user_id: str,
                              start_date: datetime,
                              end_date: datetime,
                              page_size: int = 500):
    """Iterate calendar events for the user within [start_date, end_date]."""

    start_offset = 0
    while True:
        params = {
            "start_date": _ymd(start_date),
            "end_date": _ymd(end_date),
            "start": start_offset,
            "limit": page_size,
        }
        data = scho_get(f"/users/{user_id}/events", params=params)
        items = data.get("event", []) or data.get("events", []) or data.get("data", [])
        if not items:
            break
        for ev in items:
            yield ev
        if len(items) < page_size:
            break
        start_offset += page_size


def load_sections_and_items(force_refresh=False):
    """Build section->name and item->section maps; preserve submissions. Uses cache generated_at as events start if present."""
    # Load cache; preserve submissions; early-return if fresh
    existing_assignment_submissions = {}
    cached = {}
    try:
        if (raw := CACHE_FILE.read_text()) and isinstance(cached := json.loads(raw), dict):
            existing_assignment_submissions = cached.get("assignment_submissions", {})
            if not force_refresh and cached.get("section_id_to_name") and cached.get("item_id_to_section"):
                logger.info("Loaded Schoology data from fresh cache.")
                return cached["section_id_to_name"], cached["item_id_to_section"], existing_assignment_submissions
    except Exception as e:
        logger.warning(f"Cache read failed, will rebuild: {e}")

    if force_refresh:
        logger.info("Force refreshing cache due to missing items...")

    if not SCHO_USER_UID:
        abort(500, "Set SCHOOLOGY_UID to your Schoology numeric UID (string ok).")

    start_time = _time.perf_counter()

    # 1) Fetch sections (active enrollments)
    section_id_to_name: dict[str, str] = {}
    try:
        secs = scho_get(f"/users/{SCHO_USER_UID}/sections")
    except Exception as e:
        # If we're offline, keep section map empty and continue gracefully
        if is_offline_error(e):
            ind = offline_indicator(e) or NO_WIFI_MSG
            logger.info(ind)
            secs = {"section": []}
        else:
            raise
    for s in secs.get("section", []):
        sid = str(s.get("id"))
        course_title = s.get("course_title", "") or ""
        section_title = s.get("section_title", "") or ""
        title = f"{course_title} - {section_title}".strip(" -")
        if sid:
            section_id_to_name[sid] = title

    # 2) Build item map via calendar events (assignments + events)
    now_local = datetime.now(tz=CURRENT_TZ)

    # If cache has generated_at, use that as start; else use now - days_back
    cache_start = None
    if cached and (ga := cached.get("generated_at")):
        try:
            cache_start = datetime.fromisoformat(ga)
        except Exception:
            cache_start = None
    window_start = cache_start or (now_local - timedelta(days=DAYS_BACK))
    window_end = now_local + timedelta(days=DAYS_FWD)

    item_id_to_section: dict[str, str] = cached.get("item_id_to_section", {})

    try:
        for ev in _fetch_user_events_window(SCHO_USER_UID, window_start, window_end):
            # Determine owning section: prefer explicit section_id, fallback realm_id
            sid = None
            if (sid := str(ev.get("section_id") or "")) and sid in section_id_to_name:
                pass
            elif (sid := str(ev.get("realm_id") or "")) and sid in section_id_to_name:
                pass
            else:
                continue

            if eid := str(ev.get("id") or ""):
                item_id_to_section[eid] = sid
            if ev.get("type") == "assignment" and (aid := str(ev.get("assignment_id") or "")):
                item_id_to_section[aid] = sid
    except Exception as e:
        if is_offline_error(e):
            ind = offline_indicator(e) or NO_WIFI_MSG
            logger.info(ind)
        else:
            logger.error(f"User events fetch failed: {e}")

    assignment_submissions = existing_assignment_submissions
    logger.info(f"Preserved {len(assignment_submissions)} existing assignment_submissions")

    cache_blob = {
        "section_id_to_name": section_id_to_name,
        "item_id_to_section": item_id_to_section,
        "assignment_submissions": assignment_submissions,
        "generated_at": datetime.now().isoformat()
    }
    try:
        CACHE_FILE.write_text(json.dumps(cache_blob, indent=2))
        logger.info(f"Wrote Schoology cache: {CACHE_FILE}")
    except Exception as e:
        logger.error(f"Failed to write cache: {e}")

    logger.info(f"Cache build took {(_time.perf_counter() - start_time) / 1e9:.3f}s")

    return section_id_to_name, item_id_to_section, assignment_submissions


def get_submission_status(
        item_id: str,
        due_date: datetime,
        section_id: str,
        assignment_submissions: dict,
        item_type: str = "assignment"
) -> str:
    """
    Get submission status for an assignment or discussion with caching.
    Returns:
      ✅ if submitted or manually marked as done
      ⚠️ if not submitted and not overdue (assignments only)
      ‼️ if not submitted and overdue (assignments only)
      💬 if discussion not manually marked
      - if submissions disabled
      ? if unknown/error
    Only checks API for assignments not in cache or with stale cache.
    For discussions, only checks for manual marking (no API submission check).
    """
    if not SCHO_USER_UID:
        logger.debug("status_check no UID -> ?")
        return "?"

    occ_token = occurrence_token_for_due_date(due_date)
    if occ_token:
        occ_key = manual_mark_key(item_id, occ_token)
        if occ_key in MANUAL_MARKS:
            return "✅"

    if str(item_id) in MANUAL_MARKS:
        return "✅"

    overdue = due_date < datetime.now(tz=CURRENT_TZ)
    uncompleted_symbol = "‼️" if overdue else "⚠️"

    # Only use API requests if assignment can be submitted, is unsubmitted, and cache stale
    if cached_submission := assignment_submissions.get(item_id):
        if cached_submission.get("has_submission", False):
            return "✅"
        elif item_type == "discussion":
            logger.debug("status_check cached discussion -> 💬")
            return "💬"
        elif (cached_submission.get("submissions_disabled", False) or
              not cached_submission.get("allow_dropbox", True) or
              cached_submission.get("dropbox_locked", False)):
            logger.debug("status_check cached disabled -> -")
            return "-"

        if checked_at := cached_submission.get("checked_at"):
            try:
                checked_time = datetime.fromisoformat(checked_at)
                age = (_time.time() - checked_time.timestamp())
                if age <= SUBMISSION_CACHE_MAX_AGE_SECS:
                    return uncompleted_symbol
            except Exception:
                logger.debug(f"status_check cached {item_id} invalid time {checked_at} -> {uncompleted_symbol}")
                pass

    # Custom items or missing section_id: avoid API calls, base on cache/time only
    try:
        if (custom := str(section_id).lower() == "custom") or not section_id:
            if not custom:
                logger.debug(f"status_check no section {item_id} -> {uncompleted_symbol}")
            return uncompleted_symbol
    except Exception:
        logger.debug(f"status_check exception {item_id} -> ?")
        return uncompleted_symbol

    try:
        result = check_assignment_submission(item_id, section_id, SCHO_USER_UID)

        if isinstance(result, dict):
            if result.get("error"):
                logger.debug("status_check API error -> ?")
                return "?"
            has_submission = bool(result.get("has_submission", False))
            submissions_disabled = bool(result.get("submissions_disabled", False))

        assignment_submissions[item_id] = {
            "has_submission": has_submission,
            "submissions_disabled": submissions_disabled,
            "checked_at": datetime.now().isoformat()
        }

        try:
            if CACHE_FILE.exists():
                cached = json.loads(CACHE_FILE.read_text())
                cached["assignment_submissions"] = assignment_submissions
                CACHE_FILE.write_text(json.dumps(cached, indent=2))
        except Exception as e:
            logger.error(f"Failed to update submission cache: {e}")

        if submissions_disabled:
            logger.debug("status_check API disabled -> -")
            return "-"
        res = "✅" if has_submission else uncompleted_symbol
        logger.debug(f"status_check {item_id} API result -> {res}")
        return res

    except Exception as e:
        if is_offline_error(e):
            ind = offline_indicator(e) or NO_WIFI_MSG
            logger.info(ind)
            logger.debug("status_check offline -> ?")
            return "?"
        logger.error(f"Error checking submission status: {e}")
        logger.debug("status_check exception -> ?")
        return "?"


# ------------- USER DATA: manual marks ----------------

def _load_user_data() -> dict:
    try:
        if USER_DATA_FILE.exists():
            return json.loads(USER_DATA_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_user_data(d: dict) -> None:
    try:
        USER_DATA_FILE.write_text(json.dumps(d, indent=2))
    except Exception as e:
        logger.error(f"Failed to write user data: {e}")


def _get_manual_marks() -> set[str]:
    d = _load_user_data()
    mm = d.get("manual_done") or {}
    if isinstance(mm, dict):
        return {k for k, v in mm.items() if v}
    if isinstance(mm, list):
        return set(map(str, mm))
    return set()


MANUAL_MARKS = _get_manual_marks()


def mark_item_as_done(item_id: str, occurrence_token: str | None = None):
    """
    Mark an assignment or discussion as manually completed by storing it in the cache.
    """
    try:
        d = _load_user_data()
        manual = d.get("manual_done") or {}
        key = manual_mark_key(item_id, occurrence_token)
        manual[key] = True
        d["manual_done"] = manual
        _save_user_data(d)
        MANUAL_MARKS.add(key)

        if occurrence_token:
            logger.info(f"Marked item {item_id} (occ={occurrence_token}) as done")
        else:
            logger.info(f"Marked item {item_id} as done")

    except Exception as e:
        logger.error(f"Error marking item as done: {e}")
        raise


def unmark_item_as_done(item_id: str, occurrence_token: str | None = None):
    """
    Remove manual completion marking for an assignment or discussion.
    This allows the system to re-check the actual submission status.
    """
    try:
        d = _load_user_data()
        manual = d.get("manual_done") or {}
        key = manual_mark_key(item_id, occurrence_token)
        if key in manual:
            manual.pop(key, None)
            if occurrence_token:
                logger.info(f"Unmarked item {item_id} (occ={occurrence_token}) as done")
            else:
                logger.info(f"Unmarked item {item_id} as done")
        else:
            logger.info(f"Item {item_id} ({occurrence_token or 'all'}) was not marked as done")
        d["manual_done"] = manual
        _save_user_data(d)
        MANUAL_MARKS.discard(key)

    except Exception as e:
        logger.error(f"Error unmarking item as done: {e}")
        raise


# Load Schoology maps at startup
logger.info("Building Schoology data...")
SECTION_ID_TO_NAME, ITEM_ID_TO_SECTION, ASSIGNMENT_SUBMISSIONS = load_sections_and_items()
logger.info(f"Local timezone: {CURRENT_TZ}")
