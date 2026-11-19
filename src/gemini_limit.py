#!/usr/bin/env python3
# LIMIT WAIT: when the model of a request (GEMINI_WEB_MODEL) hits its usage limit, Gemini
# silently answers on a smaller model. In wait mode (GEMINI_LIMIT_MODE=wait, default)
# the proxy parks agent turns instead, until the limit resets (time read from the
# page), then continues in the same Gemini chat. This module: reset-time parser +
# the persisted limit state (survives proxy restarts). No pyatspi here.
import contextvars, json, os, re, time
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def _local_tz():
    """System time zone (/etc/localtime), unless GEMINI_LIMIT_TZ is set."""
    name = os.environ.get("GEMINI_LIMIT_TZ")
    if name:
        return ZoneInfo(name)
    try:
        real = os.path.realpath("/etc/localtime")
        return ZoneInfo(real.split("zoneinfo/", 1)[1])
    except Exception:
        return datetime.now().astimezone().tzinfo


TZ = _local_tz()
MODE = os.environ.get("GEMINI_LIMIT_MODE", "wait").strip().lower()  # wait | lite
_VAR = Path(__file__).parent / "var"
STATE_FILE = os.environ.get("GEMINI_LIMIT_STATE", str(_VAR / "gemini_limit_state.json"))
LOG_FILE = os.environ.get("GEMINI_LIMIT_LOG", str(_VAR / "limit.log"))
PROBE_S = float(os.environ.get("GEMINI_LIMIT_PROBE_S", "900"))     # no time on the page -> probe every 15 min
MARGIN_S = float(os.environ.get("GEMINI_LIMIT_MARGIN_S", "90"))    # wake this long after reset_at
RETRY_S = float(os.environ.get("GEMINI_LIMIT_RETRY_S", "300"))     # reset time passed, still limited
MAX_AHEAD_S = 8 * 86400                                             # a parsed time further out = parse error

# what marks a limit in the page text (banner, menu item of the target model)
LIMIT_RE = re.compile(os.environ.get("GEMINI_UI_LIMIT_TEXT_RE", r"(limit\s+(reached|resets|will\s+reset)|usage\s+limit|resets?\s+(at|on|in)\b)"), re.I)

_MONTHS = {m: i + 1 for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split())}
# other UI languages: GEMINI_UI_MONTHS = 12 comma-separated month prefixes (3+ letters), Jan..Dec
for _i, _m in enumerate(x.strip().lower() for x in os.environ.get("GEMINI_UI_MONTHS", "").split(",")):
    if _m and _i < 12:
        _MONTHS[_m[:3]] = _i + 1
_MON = r"(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\w*\.?"
_TIME_RE = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})(?!\d)\s*(a\.?\s?m\.?|p\.?\s?m\.?)?", re.I)
_DAY_MON_RE = re.compile(r"(?<!\d)(\d{1,2})\s+" + _MON + r"(?:\s+(\d{4}))?", re.I)
_MON_DAY_RE = re.compile(_MON + r"\s+(\d{1,2})(?!\d)(?:,?\s+(\d{4}))?", re.I)
_TOMORROW_RE = re.compile(os.environ.get("GEMINI_UI_TOMORROW_RE", "tomorrow"), re.I)
_IN_RE = re.compile(r"\bin\s+(?:(\d+)\s*(?:h|hr|hrs|hours?))?\s*(?:(\d+)\s*(?:m|min|mins|minutes?))?", re.I)


def is_limit_text(text):
    return bool(text and LIMIT_RE.search(text))


def parse_reset(text, now=None):
    """Absolute reset time (aware datetime, GEMINI_LIMIT_TZ) from a Gemini limit text,
    or None. Handles 'Resets 11:38', 'Limit resets Nov 16, 7:38 PM', '16 Nov, 11:38',
    'tomorrow at 9:05 AM', 'in 2 h 5 min'."""
    if not text:
        return None
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    now = now.astimezone(TZ)
    m = _IN_RE.search(text)
    if m and (m.group(1) or m.group(2)) and not _TIME_RE.search(text):
        return now + timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    tm = next((t for t in _TIME_RE.finditer(text) if ":" in t.group(0)), None) or _TIME_RE.search(text)
    if not tm:
        return None
    hh, mm, ampm = int(tm.group(1)), int(tm.group(2)), (tm.group(3) or "").lower().replace(".", "").replace(" ", "")
    if ampm == "pm" and hh < 12:
        hh += 12
    elif ampm == "am" and hh == 12:
        hh = 0
    if hh > 23 or mm > 59:
        return None
    day = None
    dm = _DAY_MON_RE.search(text)
    md = _MON_DAY_RE.search(text) if not dm else None
    if dm:
        d, mon, yr = int(dm.group(1)), _MONTHS[dm.group(2).lower()[:3]], dm.group(3)
    elif md:
        mon, d, yr = _MONTHS[md.group(1).lower()[:3]], int(md.group(2)), md.group(3)
    if dm or md:
        y = int(yr) if yr else now.year
        try:
            day = datetime(y, mon, d, tzinfo=TZ).date()
        except ValueError:
            return None
        if not yr and (day - now.date()).days < -180:  # 'Jan 2' seen on Dec 31
            day = day.replace(year=y + 1)
    elif _TOMORROW_RE.search(text):
        day = (now + timedelta(days=1)).date()
    if day is None:
        day = now.date()
        cand = datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)
        if cand < now - timedelta(minutes=2):  # time only and already past -> tomorrow
            day = (now + timedelta(days=1)).date()
    res = datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)
    if (res - now).total_seconds() > MAX_AHEAD_S:
        return None
    return res


def banner_active(text, now=None):
    """Is the limit text on the page still in force? Seen live 2026-11-16 12:04: right
    at the limit the picker still reads Hydrogen (the limited item stays selected),
    so 'picker shows the target' alone is no proof. A time-only banner whose time
    passed less than 12 h ago is stale (page not refreshed yet), not tomorrow."""
    if not is_limit_text(text):
        return False
    now = (now or datetime.now(TZ)).astimezone(TZ)
    if _DAY_MON_RE.search(text) or _MON_DAY_RE.search(text) or _TOMORROW_RE.search(text) or _IN_RE.search(text) and not _TIME_RE.search(text):
        ra = parse_reset(text, now)
        return ra is None or ra > now - timedelta(minutes=2)
    tm = next((t for t in _TIME_RE.finditer(text) if ":" in t.group(0)), None)
    if not tm:
        return True  # limit text without a time: still limited
    ra = parse_reset(text, now)
    if ra is None:
        return True
    if ra.date() == now.date():
        return True  # later today
    today = ra - timedelta(days=1)
    return (now - today).total_seconds() >= 12 * 3600  # passed < 12 h ago -> stale


# ---------------------------------------------------------------- state machine
def empty():
    return {"limited": False, "since": None, "reset_at": None, "text": "", "next_check": None,
            "checks": 0, "notified": {}, "cleared_at": None}


# One state per model (key 'hydrogen' | 'auto' | 'neon'; old 'lite' / 'argon' kept).
# Hydrogen keeps the old file name (state survives the update); a Hydrogen limit does not park Auto turns
# and the other way round.
NAMES = {"hydrogen": "Hydrogen", "auto": "Auto", "neon": "Neon", "lite": "Flash-Lite", "argon": "Argon"}


def _key(name):
    """Model key for a key or display name ('Neon' -> 'neon')."""
    n = (name or "").strip().lower()
    return next((k for k, v in NAMES.items() if n in (k, v.lower())), n) or "hydrogen"


# Default model key: env GEMINI_WEB_MODEL (default Hydrogen), as before.
DEFAULT_MODEL = _key(os.environ.get("GEMINI_WEB_MODEL", "Hydrogen"))
# Model of the current request (set by the proxy per request); load / save /
# park_notice without an explicit model use it.
MODEL = contextvars.ContextVar("gwp_limit_model", default=DEFAULT_MODEL)


def state_path(model=None):
    m = _key(model or MODEL.get())
    if m == "hydrogen":
        return STATE_FILE
    root, ext = os.path.splitext(STATE_FILE)
    return "%s.%s%s" % (root, re.sub(r"[^a-z0-9_-]", "", m), ext or ".json")


def models_with_state():
    """Model keys that have a state file (the scheduler walks them)."""
    out = ["hydrogen"]
    root, ext = os.path.splitext(STATE_FILE)
    d, base = os.path.split(root)
    try:
        for f in sorted(os.listdir(d or ".")):
            m = re.match(re.escape(base) + r"\.([a-z0-9_-]+)" + re.escape(ext or ".json") + "$", f)
            if m and m.group(1) not in out:
                out.append(m.group(1))
    except OSError:
        pass
    return out


def load(path=None, model=None):
    try:
        with open(path or state_path(model), encoding="utf-8") as f:
            st = json.load(f)
        base = empty()
        base.update(st if isinstance(st, dict) else {})
        return base
    except Exception:
        return empty()


def save(st, path=None, model=None):
    path = path or state_path(model)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def on_limit(st, text, now=None):
    """Limit seen (bridge exit 5 / probe). Sets reset_at + next_check. Returns st."""
    now_t = time.time() if now is None else now
    ra = parse_reset(text, datetime.fromtimestamp(now_t, TZ))
    if not st.get("limited"):
        st.update({"limited": True, "since": now_t, "checks": 0, "notified": {}})
    st["text"] = (text or "")[:160]
    st["reset_at"] = ra.timestamp() if ra else None
    if ra and ra.timestamp() + MARGIN_S > now_t:
        st["next_check"] = ra.timestamp() + MARGIN_S
    elif ra:  # page still shows a time that has passed: Gemini lags, look again soon
        st["next_check"] = now_t + RETRY_S
    else:
        st["next_check"] = now_t + PROBE_S
    return st


def on_probe(st, ok, text="", now=None):
    """Wake-up probe result: ok -> cleared; else re-read the time and re-arm."""
    now_t = time.time() if now is None else now
    st["checks"] = int(st.get("checks") or 0) + 1
    if ok:
        st.update({"limited": False, "next_check": None, "cleared_at": now_t, "notified": {}})
        return st
    old = st.get("reset_at")
    on_limit(st, text or st.get("text") or "", now_t)
    if old and st.get("reset_at") and abs(st["reset_at"] - old - 86400) < 180:
        # time-only text that has not been refreshed yet reads as "tomorrow": Gemini lags
        st["reset_at"], st["next_check"] = old, now_t + RETRY_S
    return st


def limited(st):
    return bool(st.get("limited"))


def due(st, now=None):
    now_t = time.time() if now is None else now
    return limited(st) and (st.get("next_check") or 0) <= now_t


def hhmm(st):
    """Reset time for people: '14:38', 'tomorrow 09:05', '18.11 09:05'; '' if unknown."""
    ra = st.get("reset_at")
    if not ra:
        return ""
    r, n = datetime.fromtimestamp(ra, TZ), datetime.now(TZ)
    if r.date() == n.date():
        return r.strftime("%H:%M")
    if r.date() == (n + timedelta(days=1)).date():
        return "tomorrow " + r.strftime("%H:%M")
    return r.strftime("%d.%m %H:%M")


PARK_TEXT = os.environ.get("GEMINI_LIMIT_PARK_TEXT",
                           "Gemini {model} hit its usage limit, {when}, will continue automatically.")
RESUME_TEXT = os.environ.get("GEMINI_LIMIT_RESUME_TEXT", "Limit reset, continuing.")


def park_notice(st, model=None):
    t = hhmm(st)
    when = ("resets at %s" % t) if t else "no reset time on the page, checking every %d min" % int(PROBE_S // 60)
    m = _key(model or MODEL.get())
    return "\u23f8 " + PARK_TEXT.format(model=NAMES.get(m, m), when=when, time=t or "?") + "\n\n"


RESUME_NOTICE = "\u25b6\ufe0f " + RESUME_TEXT + "\n\n"
NOTICE_RE = re.compile("(" + re.escape("\u23f8 ") + "[^\n]*\n*|" + re.escape(RESUME_NOTICE.strip()) + "\n*)")


def strip_notices(text):
    """Our park/resume lines inside assistant history are not for Gemini."""
    return NOTICE_RE.sub("", text) if isinstance(text, str) else text


def log(msg):
    line = "%s %s\n" % (datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
