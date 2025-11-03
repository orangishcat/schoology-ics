from collections import defaultdict
from typing import Literal

from flask import Flask, request, Response, redirect, url_for, render_template, flash
from icalendar import Calendar

import utils_custom_events
from ical_helpers import *
from schoology_api_helpers import *

try:
    import setproctitle

    setproctitle.setproctitle("sCal")
except Exception:
    pass

# ------------------ APP ------------------
app = Flask(__name__, template_folder=str((Path(__file__).parent / "templates").resolve()))
app.secret_key = "scal-secret-key"


def process_event(ev: Event, assignment_stack_times: defaultdict) -> tuple[Literal["invalid", "missing", "valid", "old", "new"], Event, str | None, datetime | None]:
    ev = ev.copy()
    fields = [
        str(ev.get("URL", "")),
        str(ev.get("DESCRIPTION", "")),
        str(ev.get("SUMMARY", "")),
        str(ev.get("LOCATION", "")),
    ]
    item_id = None
    item_type = None

    for f in fields:
        # assignments / events
        m = RE_ASSIGN_OR_EVENT.search(f)
        if m:
            item_id = m.group("id")
            item_type = m.group("type")
            break
        # discussions
        m = RE_DISCUSSION.search(f)
        if m:
            item_id = m.group("id")
            item_type = "discussion"
            break
        # fallback: any Schoology item type/id so we can still stack
        m = RE_ANY_SCHO_ITEM.search(f)
        if m:
            item_id = m.group("id")
            item_type = m.group("type")
            break

    if not item_id:
        logger.warning(f"Event does not have Schoology URL: {ev}")
        return "invalid", ev, item_id, None

    # Adjust DTSTART/DTEND time on this event
    dtstart = ev.get("DTSTART")
    if not dtstart:
        logger.warning(f"Event does not have DTSTART: {ev}")
        return "invalid", ev, item_id, None
    if (now_local := datetime.now(tz=CURRENT_TZ)) - (
            event_start := dtstart.dt.astimezone(CURRENT_TZ)) > timedelta(days=DAYS_BACK):
        return "old", ev, item_id, event_start
    elif event_start - now_local > timedelta(days=DAYS_FWD):
        return "new", ev, item_id, event_start

    sdt = dtstart.dt.astimezone(CURRENT_TZ)
    sid = ITEM_ID_TO_SECTION.get(item_id)

    course_title = None
    desired = None

    if sid:
        course_title = SECTION_ID_TO_NAME.get(sid)
        if course_title:
            ev["LOCATION"] = f"{course_title.split(' - ')[0]}"

    if get_stack_events():
        desired = assignment_stack_times[sdt.date()].time()
    elif course_title:
        desired = course_due_time(course_title) if course_title else None

    if desired:
        set_due_time(ev, sdt, desired)

    sub_status = get_submission_status(ev, item_id, sdt, sid, item_type)
    clean_description(ev, item_id, item_type, sdt, sid, sub_status)
    add_status_symbol(ev, item_type, sub_status)
    return "valid" if sid else "missing", ev, item_id, sdt

@app.get("/fetch")
@logger.catch
def proxy_ics():
    """
    /?url=<ICS_URL>[&all_day_1159=1]
    - Fetches an ICS, retimes Schoology assignment/event/discussion entries per COURSE_DUE_TIMES.
    - Optional: convert 11:59pm entries to true all-day before retiming.
    """
    src = request.args.get("url")
    if not src:
        abort(400, "Provide ?url=<ICS URL>")

    # Fetch ICS
    try:
        r = requests.get(src, timeout=30)
    except Exception as e:
        if is_offline_error(e):
            ind = offline_indicator(e) or NO_WIFI_MSG
            logger.info(ind)
            return Response(NO_WIFI_MSG, status=503)
        raise
    if r.status_code != 200 or not r.content:
        abort(502, "Failed to fetch ICS")
    cal = Calendar.from_ical(r.content)

    current_time = datetime.now(tz=CURRENT_TZ)
    _stack_start = get_stack_start_time()
    assignment_stack_times = defaultdict(
        lambda: current_time.replace(hour=_stack_start.hour, minute=_stack_start.minute))

    # Track missing items and cache refresh state
    missing_items = []
    cache_refreshed = False

    def process_events():
        """Process all events, collecting missing items"""
        nonlocal missing_items
        missing_items = []
        old_events = 0
        new_events = 0

        for ev in cal.walk("VEVENT"):
            res, ev_new, item_id, sdt = process_event(ev, assignment_stack_times)
            match res:
                case "invalid":
                    continue
                case "missing":
                    missing_items.append((item_id, ev))
                case "old":
                    old_events += 1
                case "new":
                    new_events += 1
                case "valid":
                    assignment_stack_times[sdt.date()] += EVENT_LENGTH
                    ev.update(ev_new)

        logger.info(f'Skipped {old_events} old events and {new_events} new events.')

    process_events()

    # Append custom events (if any)
    try:
        from utils_custom_events import load_custom_events, add_custom
        custom_events = load_custom_events()
        for cev in custom_events:
            try:
                ve = add_custom(cev, assignment_stack_times)
                if ve is None:
                    continue
                # Support multiple events for repeats with per-date stacking
                if isinstance(ve, (list, tuple)):
                    for v in ve:
                        try:
                            cal.add_component(v)
                        except Exception:
                            continue
                else:
                    cal.add_component(ve)
            except Exception:
                continue
    except Exception:
        pass

    if missing_items and not cache_refreshed:
        new_items = [
            f"{ev.get("SUMMARY")} {ev.get("DTSTART")}" for _, ev in missing_items if
            (event_start := ev.get("DTSTART")) and event_start.dt.astimezone(CURRENT_TZ) > datetime.now(tz=CURRENT_TZ)
        ]

        logger.info(
            f"{len(missing_items)} items missing, {new_items} are new")

        if new_items:
            refresh_cache()

            for item_id, ev in missing_items:
                sid = ITEM_ID_TO_SECTION.get(item_id)
                if not sid:
                    logger.info(f"Item {item_id}, {ev.get("DESCRIPTION", "")} still not found after cache refresh")
                else:
                    logger.info(f"Successfully found item {item_id} after cache refresh")

                match process_event(ev, assignment_stack_times):
                    case "valid" | "missing", ev_new, _, sdt:
                        assignment_stack_times[sdt.date()] += EVENT_LENGTH
                        ev.update(ev_new)
                        cal.add_component(ev)
                    case _:
                        continue

    return Response(cal.to_ical(), mimetype="text/calendar; charset=utf-8")


@app.get("/api/mark-done/<item_id>")
def mark_item_done(item_id):
    """
    Mark an assignment or discussion as done by storing it in the cache.
    This endpoint is called when the user clicks the "mark as done" link.
    """
    try:
        # Mark the item as manually completed
        occurrence_token = request.args.get("occ") or None
        mark_item_as_done(item_id, occurrence_token)

        # Redirect to a simple OK page to avoid cluttering history
        return redirect(url_for('ok_page'), code=303)
    except Exception as e:
        return render_template("error.html", title="Error", message=f"Failed to mark item as done: {str(e)}"), 500


@app.get("/api/unmark-done/<item_id>")
def unmark_item_done(item_id):
    """
    Unmark an assignment or discussion as done by removing it from the cache.
    This allows the system to re-check the actual submission status.
    """
    try:
        # Unmark the item
        occurrence_token = request.args.get("occ") or None
        unmark_item_as_done(item_id, occurrence_token)

        # Redirect to a simple OK page to avoid cluttering history
        return redirect(url_for('ok_page'), code=303)
    except Exception as e:
        return render_template("error.html", title="Error", message=f"Failed to unmark item: {str(e)}"), 500


@app.get("/api/refresh-item-map")
def refresh_item_map():
    """Force refresh the cached item->section map via the Schoology API."""
    try:
        events = refresh_cache(use_cache_window=False, collect_events=True) or []
        item_map_size = len(ITEM_ID_TO_SECTION)
        section_count = len(SECTION_ID_TO_NAME)
        return render_template(
            "refresh_item_map.html",
            events=events,
            item_map_size=item_map_size,
            section_count=section_count,
            days_back=DAYS_BACK,
        ), 200
    except Exception as e:
        logger.exception("Failed to refresh Schoology cache")
        return render_template(
            "error.html",
            title="Error",
            message=f"Failed to refresh Schoology cache: {str(e)}"
        ), 500


@app.get("/ok")
def ok_page():
    return render_template("ok.html"), 200


@app.get("/mark-overdue")
def mark_overdue_assignments():
    """
    Mark all overdue (past-due) assignments as done.
    Uses Schoology events to find assignment items whose (retimed) due date is before now
    and are not already submitted, then marks them as done in the local cache.
    Returns a simple HTML summary, similar to the single-item mark page.
    """
    try:
        # Single Schoology API call to get events up to now, then mark overdue assignments as done.
        now_local = datetime.now(tz=CURRENT_TZ)
        days_back = int(os.getenv("SCHO_EVENTS_DAYS_BACK", "60"))
        window_start = (now_local - timedelta(days=days_back)).strftime("%Y-%m-%d")
        window_end = now_local.strftime("%Y-%m-%d")

        params = {
            "start_date": window_start,
            "end_date": window_end,
            "start": 0,
            "limit": 5000,
        }
        data = scho_get(f"/users/{SCHO_USER_UID}/events", params=params)
        items = data.get("event", []) or data.get("events", []) or data.get("data", [])

        def parse_start(ev):
            val = ev.get("start") or ev.get("start_date") or ev.get("due") or ev.get("date")
            if val is None:
                d, t = ev.get("start_date"), ev.get("start_time")
                if d:
                    try:
                        if t:
                            return datetime.fromisoformat(f"{d} {t}").replace(tzinfo=CURRENT_TZ)
                        return datetime.fromisoformat(str(d)).replace(tzinfo=CURRENT_TZ)
                    except Exception:
                        return None
                return None
            try:
                # epoch
                if isinstance(val, (int, float)) or (isinstance(val, str) and val.isdigit()):
                    return datetime.fromtimestamp(int(val), tz=CURRENT_TZ)
                s = str(val).replace("Z", "+00:00")
                dt = None
                try:
                    dt = datetime.fromisoformat(s)
                except Exception:
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                        try:
                            dt = datetime.strptime(s, fmt)
                            break
                        except Exception:
                            continue
                if not dt:
                    return None
                return dt if dt.tzinfo else dt.replace(tzinfo=CURRENT_TZ)
            except Exception:
                return None

        marked_ids = []
        for ev in items:
            try:
                if str(ev.get("type", "")).lower() != "assignment":
                    continue
                aid = str(ev.get("assignment_id") or ev.get("id") or "").strip()
                if not aid:
                    continue
                sdt = parse_start(ev)
                if not sdt or sdt >= now_local:
                    continue
                mark_item_as_done(aid)
                marked_ids.append(aid)
            except Exception:
                continue

        return render_template(
            "mark_overdue.html",
            marked_ids=marked_ids,
            total=len(marked_ids)
        ), 200

    except Exception as e:
        return render_template("error.html", title="Error",
                               message=f"Failed to mark overdue assignments: {str(e)}"), 500


# ------------------ Homepage: Dashboard --------------

from utils_custom_events import load_custom_events, save_custom_events


@app.get("/")
def home():
    """Dashboard uses local cache only (no network)."""
    try:
        # Pull counts from cached assignment_submissions only
        cached = {}
        if CACHE_FILE.exists():
            try:
                cached = json.loads(CACHE_FILE.read_text())
            except Exception:
                cached = {}
        subs = cached.get("assignment_submissions") or {}

        manual_item_ids = {
            str(item_id)
            for item_id, mark_val in MANUAL_MARKS.items()
            if (isinstance(mark_val, dict) and any(mark_val.values())) or (not isinstance(mark_val, dict) and bool(mark_val))
        }

        submitted = 0
        unsubmitted = 0
        # Overdue cannot be determined without due dates; keep as 0 using cache-only.
        overdue = 0

        for _iid, info in subs.items():
            try:
                if str(_iid) in manual_item_ids or info.get("has_submission"):
                    submitted += 1
                elif info.get("submissions_disabled"):
                    # Exclude disabled from unsubmitted
                    pass
                else:
                    unsubmitted += 1
            except Exception:
                continue

        total = submitted + unsubmitted

        return render_template("home.html", metrics={
            "total": total,
            "submitted": submitted,
            "overdue": overdue,
            "unsubmitted": unsubmitted,
        })
    except Exception as e:
        return render_template("error.html", title="Error", message=f"Failed to load dashboard: {str(e)}"), 500


# ------------------ Custom Events Page --------------

@app.get("/custom")
def custom_page():
    now_local = datetime.now(tz=CURRENT_TZ)
    if utils_custom_events.last_cached is not None and now_local - utils_custom_events.last_cached > timedelta(hours=12):
        load_custom_events.cache_clear()

    events = load_custom_events()
    return render_template(
        "custom.html",
        events=events[:50],
        current_date=now_local.strftime("%Y-%m-%d"),
    )


@app.post("/custom/add")
def add_custom():
    form = request.form
    name = (form.get("event_name") or form.get("name") or "").strip()
    description = (form.get("event_description") or form.get("description") or "").strip()
    course_name = (form.get("course_name") or "").strip()
    repeat = (form.get("repeat") or "none").strip().lower()
    item_type = (form.get("type") or "event").strip().lower()
    date_str = (form.get("event_date") or form.get("date") or "").strip()
    time_str = (form.get("event_time") or form.get("time") or "").strip()

    if not name or not date_str:
        flash("Name and date are required.", "error")
        return redirect(url_for("custom_page"))

    events = load_custom_events()
    import time as _t
    eid = f"cst-{int(_t.time() * 1000)}"
    events.append({
        "id": eid,
        "name": name,
        "description": description,
        "course_name": course_name,
        "type": "assignment" if item_type == "assignment" else "event",
        "date": date_str,
        "time": time_str,
        "repeat": repeat
    })
    save_custom_events(events)
    flash("Custom event added.", "ok")
    return redirect(url_for("custom_page"))


@app.post("/custom/delete/<event_id>")
def delete_custom(event_id):
    events = load_custom_events()
    new_events = [e for e in events if str(e.get("id")) != str(event_id)]
    save_custom_events(new_events)
    flash("Custom event removed.", "ok")
    return redirect(url_for("custom_page"))


@app.get("/custom/edit/<event_id>")
def edit_custom_get(event_id):
    events = load_custom_events()
    ev = next((e for e in events if str(e.get("id")) == str(event_id)), None)
    if not ev:
        flash("Event not found.", "error")
        return redirect(url_for("custom_page"))
    # Ensure keys present for template
    ev.setdefault("name", "")
    ev.setdefault("course_name", "")
    ev.setdefault("description", "")
    ev.setdefault("type", "event")
    ev.setdefault("date", "")
    ev.setdefault("time", "")
    ev.setdefault("repeat", "none")
    return render_template("custom_edit.html", event=ev)


@app.post("/custom/edit/<event_id>")
def edit_custom_post(event_id):
    try:
        events = load_custom_events()
        idx = next((i for i, e in enumerate(events) if str(e.get("id")) == str(event_id)), None)
        if idx is None:
            flash("Event not found.", "error")
            return redirect(url_for("custom_page"))

        form = request.form
        name = (form.get("event_name") or form.get("name") or "").strip()
        description = (form.get("event_description") or form.get("description") or "").strip()
        course_name = (form.get("course_name") or "").strip()
        repeat = (form.get("repeat") or "none").strip().lower()
        item_type = (form.get("type") or "event").strip().lower()
        date_str = (form.get("event_date") or form.get("date") or "").strip()
        time_str = (form.get("event_time") or form.get("time") or "").strip()

        if not name or not date_str:
            flash("Name and date are required.", "error")
            return redirect(url_for("edit_custom_get", event_id=event_id))

        # Update in place, preserve id and any legacy keys (like url)
        ev = events[idx]
        ev.update({
            "name": name,
            "description": description,
            "course_name": course_name,
            "type": "assignment" if item_type == "assignment" else "event",
            "date": date_str,
            "time": time_str,
            "repeat": repeat
        })
        save_custom_events(events)
        flash("Event updated.", "ok")
        return redirect(url_for("custom_page"))
    except Exception as e:
        return render_template("error.html", title="Error", message=f"Failed to update event: {str(e)}"), 500


# ------------------ Settings Page --------------

@app.get("/settings")
def settings_page():
    try:
        return render_template("settings.html", settings={
            "stack_events": get_stack_events(),
            "stack_start_time": get_stack_start_time().strftime("%H:%M"),
        })
    except Exception as e:
        return render_template("error.html", title="Error", message=f"Failed to load settings: {str(e)}"), 500


@app.post("/settings")
def update_settings_page():
    try:
        stack_mode = request.form.get("stack_events")  # 'on' or None
        start_time = (request.form.get("stack_start_time") or "").strip()
        update_settings(stack_events=(stack_mode == "on"), stack_start_time=start_time)
        flash("Settings updated.", "ok")
        return redirect(url_for("settings_page"))
    except Exception as e:
        return render_template("error.html", title="Error", message=f"Failed to save settings: {str(e)}"), 500


@app.get("/globals.css")
def globals_css():
    return app.send_static_file("globals.css")


# ------------------ MAIN ----------------------
if __name__ == "__main__":
    # Apple Calendar upgrades to HTTPS; run with TLS
    if not (Path(CERT_PATH).exists() and Path(KEY_PATH).exists()):
        raise SystemExit(
            f"Missing TLS cert or key.\n"
            f"Expected:\n  CERT_PATH={CERT_PATH}\n  KEY_PATH={KEY_PATH}\n"
            f"Tip (mkcert): mkcert 127.0.0.1  -> place files under certificates/"
        )

    logger.info("Launched")
    app.run(HOST, PORT, debug=DEBUG, ssl_context=(CERT_PATH, KEY_PATH))
