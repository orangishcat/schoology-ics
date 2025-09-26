import json
import os
import re
import sys
from datetime import datetime, timedelta, time
from pathlib import Path

from loguru import logger
import logging
import socket
try:
    import requests
    from requests.exceptions import ConnectionError as ReqConnectionError, Timeout as ReqTimeout, RequestException
except Exception:  # requests may not be imported yet in some contexts
    requests = None
    ReqConnectionError = tuple()
    ReqTimeout = tuple()
    RequestException = tuple()

if not os.getenv("COURSE_DUE_TIMES_JSON"):
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

COURSE_DUE_TIMES = json.loads(os.environ.get("COURSE_DUE_TIMES_JSON", "{}"))  # {"Course Substring":"HH:MM", ...}
RESOURCES_DIR = Path(__file__).parent.parent / "resources"
RESOURCES_DIR.mkdir(exist_ok=True)

SCHO_BASE = "https://api.schoology.com/v1"
SCHO_CONSUMER_KEY = os.getenv("SCHOOLOGY_KEY", "")
SCHO_CONSUMER_SECRET = os.getenv("SCHOOLOGY_SECRET", "")
SCHO_USER_UID = os.getenv("SCHOOLOGY_UID", "")  # string ok

CACHE_FILE = RESOURCES_DIR / "schoology_cache.json"
USER_DATA_FILE = RESOURCES_DIR / "user_data.json"  # custom events + manual marks
CACHE_MAX_AGE_SECS = 15 * 60
SUBMISSION_CACHE_MAX_AGE_SECS = 60 * 60

CURRENT_TZ = datetime.now().astimezone().tzinfo  # local machine tz (e.g., PDT)
EVENT_LENGTH = timedelta(minutes=50)
REPEAT_DAYS = 180
DAYS_BACK=60
DAYS_FWD=60

# Defaults come from env but are now configurable at runtime via settings page.
_DEFAULT_STACK_EVENTS = os.getenv("STACK_EVENTS", "1") == "1"
_DEFAULT_STACK_START_TIME = time(hour=8, minute=25, tzinfo=CURRENT_TZ)

CERT_PATH = os.getenv("CERT_PATH", str(RESOURCES_DIR / "certificates/127.0.0.1.pem"))
KEY_PATH = os.getenv("KEY_PATH", str(RESOURCES_DIR / "certificates/127.0.0.1-key.pem"))
DEBUG = os.getenv("DEBUG", "0") == "1"
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "4588"))

LOGS_DIR = RESOURCES_DIR / "logs"

fm = (
    "<green>{time:HH:mm:ss.SSS}</green> | "
    "<white>{level.name:<5.5}</white>| "
    "<cyan>{function:<20.20}</cyan>:<yellow>{line:<4}</yellow>\t| "
    "{message}"
)

logger.remove()
logger.add(
    LOGS_DIR / "schoology-ics_{time:YYYY-MM-DD}.log",
    rotation="00:00",
    retention="10 days",
    compression="zip",
    enqueue=True,
    backtrace=True,
    format=fm,
    level="INFO",
)

# Redirect Flask/werkzeug (std logging) to Loguru
class InterceptHandler(logging.Handler):
    def emit(self, record):
        name = record.name or ""
        message = record.getMessage()
        # Preserve original Flask/Werkzeug formatting to both console and file
        if name.startswith("werkzeug") or name.startswith("flask") or name.startswith("gunicorn"):
            try:
                lvl = logger.level(record.levelname).name
            except Exception:
                lvl = record.levelno
            # Ensure newline since raw=True bypasses sink formatting
            if not message.endswith("\n"):
                message = message + "\n"
            logger.opt(raw=True).log(lvl, message)
            return

        # Default: use Loguru formatting for app logs
        try:
            lvl = logger.level(record.levelname).name
        except Exception:
            lvl = record.levelno
        logger.opt(depth=6, exception=record.exc_info).log(lvl, message)

if not DEBUG:
    # Attach intercept handler to relevant loggers
    for name in ("werkzeug", "flask.app", "gunicorn.error", "gunicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = [InterceptHandler()]
        lg.propagate = False
        lg.setLevel("INFO")

    # Also set root to intercept to catch any stray standard logs
    root_logger = logging.getLogger()
    root_logger.handlers = [InterceptHandler()]
    root_logger.setLevel("INFO")

# Add console sink as well as file sink
logger.add(sys.stdout, format=fm, level="TRACE" if DEBUG else "INFO")

# ------------------ NETWORK -------------------
NO_WIFI_MSG = "no wifi"

def offline_indicator(err: Exception) -> str | None:
    """Return the textual indicator that suggests an offline/DNS/connection issue, or None.

    Helps replace generic "no wifi" logs with the specific trigger.
    """
    try:
        if isinstance(err, (ReqConnectionError, ReqTimeout)):
            # requests error class name is sufficient
            return err.__class__.__name__
    except Exception:
        pass

    text = str(err).lower()
    indicators = (
        "nodename nor servname provided",
        "name or service not known",
        "temporary failure in name resolution",
        "failed to establish a new connection",
        "max retries exceeded",
        "newconnectionerror",
        "gaierror",
        "getaddrinfo failed",
        "no route to host",
        "network is unreachable",
        "timed out",
        "dns",
    )
    for p in indicators:
        if p in text:
            return p

    # Walk cause/context chain to spot socket-level errors
    seen = set()
    cur = err
    while cur and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, socket.gaierror):
            return "gaierror"
        try:
            cur_text = str(cur).lower()
            for p in indicators:
                if p in cur_text:
                    return p
        except Exception:
            pass
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return None

def is_offline_error(err: Exception) -> bool:
    """Best-effort detection of offline/DNS/connection issues.

    We avoid logging verbose stack traces for these transient conditions and
    instead emit a concise message.
    """
    try:
        # Direct requests exceptions
        if isinstance(err, (ReqConnectionError, ReqTimeout)):
            return True
    except Exception:
        pass

    # Common textual indicators across platforms
    text = str(err).lower()
    indicators = (
        NO_WIFI_MSG,
        "nodename nor servname provided",
        "name or service not known",
        "temporary failure in name resolution",
        "failed to establish a new connection",
        "max retries exceeded",
        "newconnectionerror",
        "gaierror",
        "getaddrinfo failed",
        "no route to host",
        "network is unreachable",
        "timed out",
        "dns",
    )
    if any(p in text for p in indicators):
        return True

    # Walk cause/context chain to spot socket-level errors
    seen = set()
    cur = err
    while cur and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, socket.gaierror):
            return True
        try:
            cur_text = str(cur).lower()
            if any(p in cur_text for p in indicators):
                return True
        except Exception:
            pass
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return False

# ------------------ SETTINGS (runtime-configurable) -------

def _read_settings() -> dict:
    try:
        if CACHE_FILE.exists():
            cached = json.loads(CACHE_FILE.read_text())
            settings = cached.get("settings") or {}
            if isinstance(settings, dict):
                return settings
    except Exception:
        pass
    return {}


def _write_settings(settings: dict) -> None:
    try:
        cached = {}
        if CACHE_FILE.exists():
            try:
                cached = json.loads(CACHE_FILE.read_text())
            except Exception:
                cached = {}
        cached["settings"] = settings
        CACHE_FILE.write_text(json.dumps(cached, indent=2))
    except Exception as e:
        logger.error(f"Failed to persist settings: {e}")


def get_stack_events() -> bool:
    settings = _read_settings()
    val = settings.get("stack_events")
    if isinstance(val, bool):
        return val
    return _DEFAULT_STACK_EVENTS


def get_stack_start_time() -> time:
    settings = _read_settings()
    tstr = settings.get("stack_start_time")
    if isinstance(tstr, str) and ":" in tstr:
        try:
            hh, mm = map(int, tstr.split(":", 1))
            return time(hour=hh, minute=mm, tzinfo=CURRENT_TZ)
        except Exception:
            pass
    return _DEFAULT_STACK_START_TIME


def update_settings(stack_events: bool | None = None, stack_start_time: str | None = None) -> None:
    settings = _read_settings()
    if stack_events is not None:
        settings["stack_events"] = bool(stack_events)
    if stack_start_time:
        # Accept HH:MM
        try:
            hh, mm = map(int, stack_start_time.split(":", 1))
            # store string form
            settings["stack_start_time"] = f"{hh:02d}:{mm:02d}"
        except Exception:
            pass
    _write_settings(settings)

# ------------------ MARK AS DONE --------------

# Base URL for mark as done links (will be the server's own URL)
MARK_DONE_BASE_URL = f"https://{HOST}:{PORT}"

# ------------------ REGEX ---------------------
# Named groups so the handler is robust.
RE_ASSIGN_OR_EVENT = re.compile(
    r'(?P<scheme>https?)://[^/]*\.schoology\.com/(?P<type>assignment|event|assessment)/(?P<id>\d+)(?:[/?#]|$)',
    re.IGNORECASE
)

# Example: http://bins.schoology.com/course/7916825598/materials/discussion/view/7927769656
RE_DISCUSSION = re.compile(
    r'(?P<scheme>https?)://[^/]*\.schoology\.com/course/\d+/materials/discussion/(?:view/)?(?P<id>\d+)(?:[/?#]|$)',
    re.IGNORECASE
)

RE_LINK_ASSIGN_OR_EVENT = re.compile(f' - Link: {RE_ASSIGN_OR_EVENT.pattern}', re.IGNORECASE)
RE_LINK_DISCUSSION = re.compile(f' - Link: {RE_DISCUSSION.pattern}', re.IGNORECASE)

# Fallback: match any Schoology item path to extract id/type when unknown
RE_ANY_SCHO_ITEM = re.compile(
    r'(?P<scheme>https?)://[^/]*\.schoology\.com/(?P<type>[a-zA-Z_-]+)/(?P<id>\d+)(?:[/?#]|$)',
    re.IGNORECASE
)
