#!/usr/bin/python3
"""
gemini_atspi_bridge.py — one-shot X11+AT-SPI bridge to Gemini Web, isolated
from the VK parser (separate profile `gemini-pump.default`, separate X
display :2). Opens Firefox only for the duration of the call, always starts
a FRESH chat session (Ctrl+Shift+O) to avoid context bleed between calls,
and tears everything down afterwards to free resources — unless --keep-open.

Usage:
  /usr/bin/python3 gemini_atspi_bridge.py "your prompt here" [--keep-open] [--timeout 40]

Requires: xdotool, Xvfb, py3-atspi (pyatspi), a Firefox profile at
~/.mozilla/firefox/gemini-pump.default already logged into Google.

Must run with the SYSTEM /usr/bin/python3 (pyatspi is not installed in any
project .venv). Never touches other displays / profiles used by other tools.
— that one belongs to vk-token-refresher and is a hard requirement of its
autonomous pipeline (see skill vk-token-refresher).
"""
import subprocess
import sys
import time
import os
import json
import re
import signal
import fcntl

# --- The only valid interpreter ---
# pyatspi is installed ONLY in the system /usr/bin/python3 (Alpine musl). Neither
# the project .venv nor .venv311 has it (both fail with ModuleNotFoundError:
# No module named 'pyatspi' — confirmed live 2026-09-30 when the bridge was run
# under .venv/bin/python). To stop anyone from launching the bridge the wrong way
# and wasting time on the same reproducible failure, the bridge runs only under
# the system python: any other environment is refused immediately with a clear
# message. This is a HARD guard, not a hint.
_IMPORT_ERROR = (
    "gemini_atspi_bridge.py must run under the SYSTEM /usr/bin/python3 "
    "(the only interpreter where pyatspi is installed). Got: %s"
)
_OK_INTERPRETERS = {"/usr/bin/python3", "/usr/bin/python3.14"}
if os.path.realpath(sys.executable) not in _OK_INTERPRETERS:
    sys.stderr.write(_IMPORT_ERROR % sys.executable + "\n")
    sys.exit(3)
try:
    import pyatspi  # noqa: F401  (guard import: fail fast here, not mid-run)
except ImportError as _e:
    sys.stderr.write(_IMPORT_ERROR % sys.executable + "\n")
    sys.exit(3)

DISPLAY = os.environ.get("GEMINI_DISPLAY", ":2")
PROFILE = os.environ.get("GEMINI_PUMP_PROFILE",
           os.path.join(os.path.expanduser("~"), ".mozilla/firefox/gemini-pump.default"))
XVFB_LOCK = f"/tmp/.X11-unix/X{DISPLAY.lstrip(':')}"
FIREFOX_BIN = os.environ.get("GEMINI_FIREFOX_BIN", "/usr/bin/firefox")
GEMINI_URL = "https://gemini.google.com/app"

# --- Gemini web UI labels (English defaults) --------------------------------
# The bridge reads the page through the accessibility tree, so it matches a few
# visible labels. Defaults are the English Gemini UI; if your Gemini UI is in
# another language, override these via env (comma-separated alternatives).
def _labels(env, default):
    return tuple(x.strip() for x in os.environ.get(env, default).split("|") if x.strip())

UI_ANSWER_HEADING = _labels("GEMINI_UI_ANSWER_HEADING", "Gemini said")
UI_REQUEST_HEADING = _labels("GEMINI_UI_REQUEST_HEADING", "You said")
UI_VOICE_BUTTON = _labels("GEMINI_UI_VOICE_BUTTON", "Start voice input|Use microphone")
UI_GOOD_RESPONSE = _labels("GEMINI_UI_GOOD_RESPONSE", "Good response")
UI_DISCLAIMER = _labels("GEMINI_UI_DISCLAIMER", "can make mistakes")


def sh(cmd, env=None, timeout=20, input=None):
    e = dict(os.environ)
    e["DISPLAY"] = DISPLAY
    if env:
        e.update(env)
    return subprocess.run(cmd, env=e, capture_output=True, text=True, timeout=timeout, input=input)


def xvfb_running():
    """Alive = lock file exists AND the PID inside it is a live process.
    Before 2026-09-30 this only checked the lock file existed; after any
    SIGKILL of Xvfb the stale /tmp/.X2-lock stayed behind, so Xvfb was never
    restarted and Firefox died with 'cannot open display :2' on every run.
    A stale lock (dead PID) is removed here together with the X socket."""
    pid_lock = "/tmp/.X%s-lock" % DISPLAY.lstrip(":")
    if not os.path.exists(pid_lock) and not os.path.exists(XVFB_LOCK):
        return False
    try:
        pid = int(open(pid_lock).read().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, OSError):
        pass
    for f in (pid_lock, XVFB_LOCK):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass
        except Exception as ex:
            print("stale X lock cleanup failed for %s: %s" % (f, ex), file=sys.stderr)
    return False


def start_xvfb():
    if xvfb_running():
        _ensure_wm()
        return
    subprocess.Popen(
        ["Xvfb", DISPLAY, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        stdout=open("/tmp/xvfb2.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(30):
        if xvfb_running():
            _ensure_wm()
            return
        time.sleep(0.3)
    raise RuntimeError("Xvfb did not come up on " + DISPLAY)


def _wm_running():
    return bool(_pids_matching(f"fluxbox -display {DISPLAY}"))


def _ensure_wm():
    """No window manager on :2 means X11 input-focus semantics are undefined
    -- confirmed live 2026-09-30 as the TRUE root cause of both xdotool type
    AND xdotool key/ctrl+v paste silently landing nowhere (real chat input
    stayed empty every time, verified by reading it back via AT-SPI right
    after each attempt). A bare Xvfb is not enough; xdotool needs a real WM
    to know which window/element currently owns keyboard input."""
    if _wm_running():
        return
    e = dict(os.environ); e["DISPLAY"] = DISPLAY
    subprocess.Popen(
        ["fluxbox", "-display", DISPLAY],
        env=e, stdout=open("/tmp/fluxbox2.log", "a"), stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(20):
        if _wm_running():
            time.sleep(0.5)  # let fluxbox take over window management
            return
        time.sleep(0.2)


def find_gemini_window():
    r = sh(["xdotool", "search", "--class", "firefox"])
    for wid in r.stdout.split():
        name = sh(["xdotool", "getwindowname", wid]).stdout.strip()
        if "Gemini" in name:
            return wid, name
    return None, None


def _memcap_prefix():
    """[claude] 2026-11-14: run the Gemini Firefox inside a user systemd scope with
    MemoryHigh (default 700M, env GEMINI_FF_MEMORY_HIGH; "0" = off) so its cold pages
    go to zram instead of eating the RAM he needs for video. Falls back to a plain
    launch if the user manager is not reachable."""
    cap = os.environ.get("GEMINI_FF_MEMORY_HIGH", "700M")
    if cap in ("", "0", "off"):
        return [], {}
    env = {"XDG_RUNTIME_DIR": "/run/user/%d" % os.getuid(),
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/%d/bus" % os.getuid()}
    try:
        r = subprocess.run(["systemd-run", "--user", "--scope", "-q", "true"],
                           env={**os.environ, **env}, capture_output=True, timeout=10)
        if r.returncode != 0:
            return [], {}
    except Exception:
        return [], {}
    return (["systemd-run", "--user", "--scope", "-q",
             "--description=Gemini bridge Firefox (memory cap)",
             "-p", "MemoryHigh=%s" % cap], env)


def start_firefox():
    _pre, _env = _memcap_prefix()
    subprocess.Popen(
        _pre + [FIREFOX_BIN, "--profile", PROFILE, "--no-remote", GEMINI_URL],
        env={**os.environ, **_env, "DISPLAY": DISPLAY},
        stdout=open("/tmp/gemini_bridge_ff.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(100):
        wid, name = find_gemini_window()
        if wid:
            return wid
        time.sleep(0.5)
    raise RuntimeError("Gemini window did not appear")


def ensure_running():
    start_xvfb()
    wid, name = find_gemini_window()
    if wid:
        return wid
    return start_firefox()


def _activate(wid):
    """Grab X input focus onto the Gemini window. Without a real WM the
    active-window semantics are undefined, and even WITH fluxbox the
    `xdotool key --window` variant can still be silently dropped by Firefox
    when the target window isn't the currently-focused one (confirmed live
    2026-09-30: new-chat shortcut AND ctrl+v paste both went nowhere because
    the focused window was Firefox's 'Library' window, not the chat). Bring
    the window to front + activate, then send GLOBAL keys (no --window)."""
    sh(["xdotool", "windowactivate", "--sync", wid], timeout=10)
    time.sleep(0.4)

def new_chat_session(wid):
    """Ctrl+Shift+O = Gemini's 'new chat' shortcut. MUST be sent before every
    unrelated prompt, otherwise the previous conversation's context leaks
    into the next answer (user-flagged requirement, 2026-09-29)."""
    _activate(wid)
    sh(["xdotool", "key", "--clearmodifiers", "ctrl+shift+o"])
    # [claude] 2026-11-14 PERF: adaptive instead of a blind 1.5s - wait until the
    # address bar drops the old /app/<id> (= fresh chat open), cap 1.6s.
    global _ENTRY_CACHE
    _ENTRY_CACHE = None
    time.sleep(0.4)
    _dl = time.time() + 1.6
    while time.time() < _dl:
        if not get_current_conv_id():
            break
        time.sleep(0.15)
    # On a hung page the shortcut does nothing and the "fresh" prompt went into
    # the OLD chat. Still on a /app/<id> -> open the new-chat URL directly.
    if get_current_conv_id():
        print("[new chat] shortcut did not leave the old chat - opening /app directly",
              file=sys.stderr)
        _activate(wid)
        sh(["xdotool", "key", "--clearmodifiers", "ctrl+l"])
        time.sleep(0.3)
        sh(["xdotool", "type", "--clearmodifiers", "--delay", "5", "https://gemini.google.com/app"],
           timeout=20)
        sh(["xdotool", "key", "--clearmodifiers", "Return"])
        _dl = time.time() + 8
        while time.time() < _dl:
            time.sleep(0.5)
            if not get_current_conv_id():
                break
        time.sleep(1.5)  # let the empty chat render its input
        _ENTRY_CACHE = None
        if get_current_conv_id():
            print("[new chat] FAILED: still in an old chat - exit 9", file=sys.stderr)
            sys.exit(9)


_ENTRY_CACHE = None  # [claude] 2026-11-14 PERF: one AT-SPI walk per bridge run, not per message


def _cached_entry():
    """Reuse the chat-input node found earlier in THIS run (activation ->
    prompt): saves a full AT-SPI walk (~1.2s). Falls back to a fresh walk
    when the node is gone/hidden."""
    global _ENTRY_CACHE
    e = _ENTRY_CACHE
    if e is not None:
        try:
            import pyatspi
            if e.getRoleName() == "entry" and e.getState().contains(pyatspi.STATE_SHOWING):
                return e, True
        except Exception:
            pass
    _ENTRY_CACHE = find_real_entry()
    return _ENTRY_CACHE, False


def find_real_entry():
    """The AT-SPI accessible node for Gemini's actual chat input (not
    Firefox's address bar, which is ALSO role=entry and sits earlier in the
    tree -- confirmed live 2026-09-30 that a naive first-match walk grabs the
    address bar instead). Returns None if not found (caller falls back to
    sending keys to the window as a whole)."""
    import pyatspi

    def find_app(name):
        d = pyatspi.Registry.getDesktop(0)
        for a in d:
            if a.name == name:
                return a
        return None

    # Since a Gemini UI update (Nov 2026) the page has TWO entries with the same
    # name (an inert one first, the real input later). The first match was the
    # inert one, so paste checks read 0 chars while the text was in the real
    # field. Collect all and prefer focused > showing+visible > the last one.
    found = []

    def walk(node):
        try:
            role = node.getRoleName()
            name = node.name
        except Exception:
            return
        if role == "entry" and "Gemini" in (name or ""):
            found.append(node)
            return
        try:
            n = node.childCount
        except Exception:
            n = 0
        for i in range(n):
            try:
                child = node.getChildAtIndex(i)
            except Exception:
                continue
            walk(child)

    app = find_app("Firefox")
    if app is None:
        return None
    walk(app)
    if not found:
        return None

    def _st(n, s):
        try:
            return n.getState().contains(s)
        except Exception:
            return False
    for n in found:
        if _st(n, pyatspi.STATE_FOCUSED):
            return n
    vis = [n for n in found if _st(n, pyatspi.STATE_SHOWING) and _st(n, pyatspi.STATE_VISIBLE)]
    return (vis or found)[-1]


def get_current_conv_id():
    """Read the active Gemini conversation id from the Firefox AT-SPI tree.

    Before 2026-09-30 the address-bar node exposed only a truncated
    ``gemini.google.com/app`` (the drow-down 'recent tabs' section node), so
    the session id was unreadable programmatically. Confirmed live 2026-09-30
    that the FULL current-tab URL -- ``gemini.google.com/app/<conv_id>`` --
    is present in the tree; regex-extract it. Returns full URL or None."""
    import re
    import pyatspi

    out = []

    def walk(node, depth=0):
        if depth > 14:
            return
        try:
            if "Text" in node.get_interfaces():
                t = node.queryText().getText(0, -1).strip()
                if t:
                    out.append(t)
        except Exception:
            pass
        try:
            c = node.childCount
        except Exception:
            return
        for i in range(c):
            try:
                ch = node.getChildAtIndex(i)
            except Exception:
                continue
            walk(ch, depth + 1)

    try:
        desktop = pyatspi.Registry.getDesktop(0)
        for app in desktop:
            if app.name == "Firefox":
                walk(app)
    except Exception:
        return None
    for t in out:
        m = re.search(r'(gemini\.google\.com/app/[0-9a-fA-F]+)', t)
        if m:
            return m.group(1)
    return None


_TURN_HEADINGS = UI_REQUEST_HEADING + UI_ANSWER_HEADING


def _nav_scan(want):
    """One pass over the Firefox tree: (target id in a gemini URL, any gemini
    URL seen, chat turns or a chat title loaded)."""
    import re
    import pyatspi
    st = {"hit": False, "url": False, "loaded": False}

    def walk(n, d=0):
        if d > 30:
            return
        try:
            role = n.getRoleName()
            if role == "document web":
                nm = (n.name or "").strip()
                if nm.endswith(" - Google Gemini"):  # a real chat has a title
                    st["loaded"] = True
            if "Text" in n.get_interfaces():
                t = n.queryText().getText(0, -1).strip()
                if role == "heading" and t.startswith(_TURN_HEADINGS):
                    st["loaded"] = True
                for m in re.finditer(r'gemini\.google\.com/app(/[0-9a-fA-F]+)?', t):
                    st["url"] = True
                    if (m.group(1) or "").lstrip("/").lower() == want:
                        st["hit"] = True
        except Exception:
            pass
        try:
            c = n.childCount
        except Exception:
            return
        for i in range(c):
            try:
                walk(n.getChildAtIndex(i), d + 1)
            except Exception:
                pass

    try:
        for app in pyatspi.Registry.getDesktop(0):
            if app.name == "Firefox":
                walk(app)
    except Exception:
        pass
    return st


def conv_is_open(conv_url, wait_s=None):
    """LOST CHAT GUARD. The address bar alone lies: right
    after Enter it shows the typed URL even for a deleted chat, Gemini redirects
    to bare /app only ~3.5-4 s later (probed live). So, polling ALL text nodes:
    - target id in the URL + chat turns or chat title loaded -> open;
    - gemini URL visible but without the target id (redirected) -> lost;
    - after GEMINI_NAV_VERIFY_S (default 6 s): open only if the URL still
      holds the target id (a redirect would have happened by then)."""
    want = conv_url.rstrip("/").split("/")[-1].lower()
    if wait_s is None:
        wait_s = float(os.environ.get("GEMINI_NAV_VERIFY_S", "6"))
    t0 = time.time()
    while True:
        st = _nav_scan(want)
        if st["hit"] and st["loaded"]:
            print("[navigate] verified open in %.1fs" % (time.time() - t0), file=sys.stderr)
            return True
        if st["url"] and not st["hit"]:
            print("[navigate] redirected away from %s" % want, file=sys.stderr)
            return False
        if time.time() - t0 >= wait_s:
            print("[navigate] verify timeout, url hit=%s" % st["hit"], file=sys.stderr)
            return st["hit"]
        time.sleep(0.3)


# --- PERF 2026-10-03: timings pulled out of fixed sleeps into constants ---
# Justified by measurements (live run, /usr/bin/time):
#   navigate_to_chat 5.4s (3.0 of it a blind pause), send_prompt 4.5s,
#   select_3_6_flash 3.6s, read_response 7.8s.
# Values are a lower bound ("enough to land"), not "how long we had to wait".
NAV_L_CTRL_L = 0.4        # was 0.8 — the address bar opens fast
NAV_L_AFTER_TYPE = 0.3    # was 0.6 — URL is short (~40 chars), type --delay 5
NAV_L_MAX_WAIT = 3.0      # cap for chat load (adaptive, not fixed)
SEL_DROPDOWN_WAIT = 0.7   # was 1.6 — model selector opening
SEL_ITEM_WAIT = 0.6       # was 1.2 — menu closing after selection
SEL_BEFORE_OPEN = 0.25    # was 0.4 — cursor positioning
SEL_BEFORE_ITEM = 0.25    # was 0.4
PASTE_SETTLE = 0.5        # was 1.0 — time for contenteditable to accept the paste
ACTIVATE_GUARD = 0.3      # was 0.3 — grabFocus

def navigate_to_chat(conv_id):
    """Reopen an existing Gemini conversation by its URL into the address bar
    (Ctrl+L -> type -> Enter). Gemini restores the exact session by link --
    this is the session-persistence mechanism (2026-09-30). Does NOT send
    Ctrl+Shift+O, so the existing context is preserved.

    Uses xdotool TYPE, NOT clipboard paste: confirmed live 2026-10-01 that
    xclip + Ctrl+V silently did NOT land the URL (address bar kept showing
    only 'gemini.google.com/app'; the full '/<conv_id>' never appeared even
    though the X clipboard held it). xdotool type works reliably for the
    short URL."""
    _activate(find_gemini_window()[0])
    sh(["xdotool", "key", "--clearmodifiers", "ctrl+l"])
    time.sleep(NAV_L_CTRL_L)
    sh(["xdotool", "type", "--clearmodifiers", "--delay", "5", conv_id], timeout=20)
    time.sleep(NAV_L_AFTER_TYPE)
    sh(["xdotool", "key", "--clearmodifiers", "Return"])
    # ADAPTIVE instead of a fixed 3.0s (PERF 2026-10-03): wait until the address
    # bar actually shows the target conv_id — that is the signal that Gemini
    # opened the right chat. Before we waited 3s blindly; the measurement showed
    # navigate_to_chat = 5.4s, 3.0 of which was a blind pause. Now usually ~1.2s,
    # same 3.0s cap (for slow render), i.e. faster and stricter: we don't wait
    # for the context to finish loading — it loads itself, and the answer is read
    # via read_response / the DOM buffer, which does exactly that.
    deadline = time.time() + NAV_L_MAX_WAIT
    loaded = False
    while time.time() < deadline:
        cur = get_current_conv_id()
        if cur and conv_id.split("/")[-1] in cur:
            loaded = True
            break
        time.sleep(0.2)
    print("[navigate] conv loaded in %.2fs (loaded=%s)"
          % (NAV_L_MAX_WAIT - (deadline - time.time()), loaded), file=sys.stderr)


def send_prompt(wid, text, clear=True):
    """Clipboard paste (xclip + Ctrl+V) into Gemini's real chat input.

    History, 2026-09-29/30: paste was briefly replaced by xdotool type after
    a flawed check read only the entry CONTAINER via AT-SPI (it shows a
    placeholder + an embedded-object char); the actual text lives in a child
    'paragraph' node. Re-measured correctly: typing does land, but real
    xdotool speed is ~40 chars/s, so a 53k-char Hermes prompt takes ~22 min
    -- unusable. Paste is length-independent, so it is back. Kept from the
    detour: AT-SPI grabFocus on the REAL input (not the address bar), a
    window manager on :2 (see _ensure_wm), and field_text_len() to verify.
    """
    _activate(wid)
    if clear:
        entry, _from_cache = _cached_entry()
    else:  # vision: images already sit in the field, never risk a retry-clear
        entry, _from_cache = find_real_entry(), False
    if entry is not None:
        try:
            entry.queryComponent().grabFocus()
            time.sleep(0.3)
        except Exception:
            pass
    if clear:  # [claude] 2026-11-12: vision keeps attached images in the field
        sh(["xdotool", "key", "--clearmodifiers", "ctrl+a"])
        sh(["xdotool", "key", "--clearmodifiers", "Delete"])
    clip_env = dict(os.environ)
    clip_env["DISPLAY"] = DISPLAY
    clip = subprocess.Popen(
        # [claude] vision: after an image paste Firefox asks TARGETS first, so a
        # one-shot (-loops 1) clipboard is used up before the text is read
        ["xclip", "-selection", "clipboard"] + (["-loops", "1"] if clear else []),
        env=clip_env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    clip.stdin.write(text.encode("utf-8"))
    clip.stdin.close()
    time.sleep(0.3)
    sh(["xdotool", "key", "--clearmodifiers", "ctrl+v"], timeout=15)
    try:
        clip.wait(timeout=5 if clear else 1.5)
    except subprocess.TimeoutExpired:
        clip.kill()
    time.sleep(PASTE_SETTLE)
    # Paste check: reuse the entry already found (don't search twice — every
    # find_real_entry is a full AT-SPI walk). The check itself ALWAYS stays: it
    # is what catches a "paste that missed" (empty field -> Enter sends nothing
    # -> Gemini never answers). For a SMALL prompt it is free; for a big one
    # (tens of KB) exactness is overkill — comparing length is enough: a 1-2
    # char mismatch is normal, a difference of orders of magnitude = missed the
    # field.
    got = field_text_len(entry)
    if len(text) > 20000:
        ok = got is not None and got > len(text) * 0.5
    else:
        # the field drops newlines - don't redo a good paste for them
        ok = got is not None and len(text) - text.count("\n") - 2 <= got <= len(text)
    print("[send_prompt] pasted %d chars, field holds %s, verified=%s%s"
          % (len(text), got, ok, " (cached entry)" if _from_cache else ""), file=sys.stderr)
    if not ok and _from_cache:
        # cached node went stale -> redo once with a fresh walk (clear=True path only)
        global _ENTRY_CACHE
        _ENTRY_CACHE = None
        print("[send_prompt] cached entry missed - retry with fresh walk", file=sys.stderr)
        return send_prompt(wid, text, clear=True)
    # A missed paste (field empty) used to be followed by Enter anyway -> nothing
    # sent -> the OLD answer was read back -> the agent looped on the same tool
    # call. Now: retry the paste with a real click into the field. If the field
    # still reads empty, press Enter anyway - the reading itself may be wrong
    # after a UI change; confirm_sent() checks the page for the real result.
    landed = got is not None and (got > len(text) * 0.5 if len(text) > 10 else got > 0)
    if clear and not landed:
        for _try in (1, 2):
            print("[send_prompt] paste missed (field %s of %d) - retry %d with click focus"
                  % (got, len(text), _try), file=sys.stderr)
            _activate(wid)
            entry = find_real_entry()
            _click_entry(entry)
            sh(["xdotool", "key", "--clearmodifiers", "ctrl+a"])
            sh(["xdotool", "key", "--clearmodifiers", "Delete"])
            clip = subprocess.Popen(["xclip", "-selection", "clipboard", "-loops", "1"],
                                    env=clip_env, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            clip.stdin.write(text.encode("utf-8"))
            clip.stdin.close()
            time.sleep(0.3)
            sh(["xdotool", "key", "--clearmodifiers", "ctrl+v"], timeout=15)
            try:
                clip.wait(timeout=5)
            except subprocess.TimeoutExpired:
                clip.kill()
            time.sleep(PASTE_SETTLE)
            got = field_text_len(entry)
            landed = got is not None and (got > len(text) * 0.5 if len(text) > 10 else got > 0)
            if landed:
                print("[send_prompt] retry %d landed: field holds %s" % (_try, got), file=sys.stderr)
                break
        if not landed:
            print("[send_prompt] paste not verified after retries - Enter anyway, page check decides",
                  file=sys.stderr)
    sh(["xdotool", "key", "--clearmodifiers", "Return"])
    return True


def _click_entry(entry):
    """Real mouse click into the chat input (focus that survives a lost grabFocus)."""
    if entry is None:
        return
    try:
        import pyatspi
        x, y, w, h = entry.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
        if w > 0 and h > 0:
            sh(["xdotool", "mousemove", str(x + w // 2), str(y + h // 2), "click", "1"])
            time.sleep(0.3)
    except Exception as exc:
        print("[send_prompt] click focus failed: %s" % exc, file=sys.stderr)


def field_text_len(entry=None):
    """Characters actually in the chat input: sum over the entry's CHILD
    nodes (the entry itself only exposes a placeholder/embedded-object char).

    ``entry`` can be passed to avoid a second full AT-SPI walk
    (send_prompt already found the node — reuse it)."""
    if entry is None:
        entry = find_real_entry()
    if entry is None:
        return None
    total = 0
    stack = [entry]
    while stack:
        n = stack.pop()
        try:
            c = n.childCount
        except Exception:
            continue
        for i in range(c):
            try:
                ch = n.getChildAtIndex(i)
            except Exception:
                continue
            try:
                total += len(ch.queryText().getText(0, -1))
            except Exception:
                pass
            stack.append(ch)
    return total

def _atspi_text(node):
    try:
        return node.queryText().getText(0, -1)
    except Exception:
        return ""


def _inline_text(node):
    """Text of `node` with U+FFFC (Object Replacement Character) resolved.

    AT-SPI puts U+FFFC into a paragraph's text wherever an embedded object
    sits -- Gemini renders inline code chips (`du -sh`) and links as such
    objects. The real text lives in the child the matching Hypertext link
    points at ([startIndex,endIndex) -> object). Without this the reply
    reached Hermes as 'commands \ufffc.' with the code strings emitted again
    as separate lines below (confirmed live 2026-10-02, both symptoms).
    Returns (text, consumed_child_indexes): consumed children were folded
    into the text and must not be emitted a second time by the tree walk.
    static -> inline code (single line gets `backticks`, as in Gemini's UI),
    link -> its text; buttons/sections/images are UI chrome and are dropped.
    """
    t = _atspi_text(node)
    if "\ufffc" not in t:
        return t, set()
    consumed = set()
    try:
        ht = node.queryHypertext()
        links = [ht.getLink(i) for i in range(ht.getNLinks())]
    except Exception:
        return t.replace("\ufffc", ""), consumed
    reps = []
    for l in links:
        try:
            o = l.getObject(0)
            r = o.getRoleName()
            if r not in ("static", "link"):
                continue
            sub, _ = _inline_text(o)
            sub = sub.replace("\ufffc", "")
            if not sub.strip():
                continue
            if r == "static" and "\n" not in sub:
                sub = "`" + sub + "`"
            reps.append((l.startIndex, l.endIndex, sub))
            consumed.add(o.getIndexInParent())
        except Exception:
            continue
    for a, b, sub in sorted(reps, reverse=True):
        t = t[:a] + sub + t[b:]
    return t.replace("\ufffc", ""), consumed


def ui_idle_state():
    """[claude] 2026-11-14: (idle, rated) from the Gemini UI. idle = the voice-input
    button is shown (it disappears while Gemini is generating); rated = number of
    finished answers (each gets a 'Good response' button)."""
    import pyatspi
    names = []

    def walk(n):
        try:
            if n.getRoleName() in ("push button", "toggle button"):
                names.append(n.name or "")
            for i in range(n.childCount):
                walk(n.getChildAtIndex(i))
        except Exception:
            pass
    for a in pyatspi.Registry.getDesktop(0):
        if a.name == "Firefox":
            walk(a)
    idle = any(any(x.startswith(v) for v in UI_VOICE_BUTTON) for x in names)
    return idle, sum(1 for x in names if x in UI_GOOD_RESPONSE)


def wait_idle(min_rated=0, timeout=45, poll=0.5):
    """Wait until Gemini finished generating (and has >= min_rated answers).
    Sending while it still generates = Enter ignored = 'empty read' later."""
    deadline = time.time() + timeout
    st = (False, 0)
    while time.time() < deadline:
        st = ui_idle_state()
        if st[0] and st[1] >= min_rated:
            return True
        time.sleep(poll)
    print("[wait_idle] timeout %ds (idle=%s rated=%s)" % (timeout, st[0], st[1]), file=sys.stderr)
    return False


_MODEL_CHECK_FILE = "/tmp/gemini_model_check.json"


def last_request_signature():
    """SEND CONFIRM: text of the LAST request block on the page (our echoed
    message under the request heading), whitespace removed. If it does not
    change after Enter, Gemini did not take the message (seen live: the old
    answer was read back again and again -> the agent looped)."""
    import pyatspi
    app = None
    for a in pyatspi.Registry.getDesktop(0):
        try:
            if a and a.name == "Firefox":
                app = a
                break
        except Exception:
            pass
    if app is None:
        return None
    out = []

    def walk(n):
        try:
            role = n.getRoleName()
        except Exception:
            return
        if role in ("heading", "paragraph", "list item", "entry"):
            try:
                t = n.queryText().getText(0, -1)
            except Exception:
                t = ""
            if t and t.strip():
                out.append((role, t))
            if role == "entry":
                return  # the input field: its text is not on the page yet
        try:
            cnt = n.childCount
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                walk(n.getChildAtIndex(i))
            except Exception:
                pass
    walk(app)
    last = None
    for i, (role, t) in enumerate(out):
        if role == "heading" and any(t.strip().startswith(h) for h in UI_REQUEST_HEADING):
            last = i
    if last is None:
        return ""
    parts = [out[last][1]]
    for role, t in out[last + 1:]:
        if role == "entry" or (role == "heading" and any(t.strip().startswith(h) for h in UI_ANSWER_HEADING)):
            break
        parts.append(t)
    return re.sub(r"\s+", "", "".join(parts).replace("\ufffc", ""))


def confirm_sent(prev_sig, timeout=None):
    """True when the last request block on the page changed after our Enter.
    One extra Enter after half the wait (a lost keypress). prev_sig None -> skip.
    GEMINI_SEND_CONFIRM=0 turns it off, GEMINI_SEND_CONFIRM_S = wait (14 s)."""
    if prev_sig is None or os.environ.get("GEMINI_SEND_CONFIRM", "1") == "0":
        return True
    timeout = timeout or float(os.environ.get("GEMINI_SEND_CONFIRM_S", "14"))
    t0, again = time.time(), False
    while time.time() - t0 < timeout:
        sig = last_request_signature()
        if sig is not None and sig != prev_sig and sig:
            print("[send] confirmed in %.1fs" % (time.time() - t0), file=sys.stderr)
            return True
        if not again and time.time() - t0 > timeout / 2:
            again = True
            print("[send] not on the page yet - pressing Enter again", file=sys.stderr)
            sh(["xdotool", "key", "--clearmodifiers", "Return"])
        time.sleep(1.0)
    return False


def _field_text(entry):
    """All text in the chat input (children), '' when unknown."""
    out, stack = [], [entry] if entry is not None else []
    while stack:
        n = stack.pop()
        try:
            cnt = n.childCount
        except Exception:
            continue
        for i in range(cnt):
            try:
                ch = n.getChildAtIndex(i)
                out.append(ch.queryText().getText(0, -1))
            except Exception:
                ch = None
            if ch is not None:
                stack.append(ch)
    return "".join(out)


def _find_menu_item(prefix):
    import pyatspi
    found = []

    def walk(n):
        if found:
            return
        try:
            r = n.getRoleName()
            nm = n.name or ""
        except Exception:
            return
        if r == "menu item" and nm.startswith(prefix):
            found.append(n)
            return
        try:
            cnt = n.childCount
        except Exception:
            cnt = 0
        for i in range(cnt):
            try:
                walk(n.getChildAtIndex(i))
            except Exception:
                pass
    for a in pyatspi.Registry.getDesktop(0):
        try:
            if a and a.name == "Firefox":
                walk(a)
                break
        except Exception:
            pass
    return found[0] if found else None


def attach_skill(wid, name):
    """SKILL CHIP: since the Gemini UI update the text trigger
    '/tool-call-formatter' is answered 'command not recognized'. Skills are now
    picked from the '@' menu and sit in the input as a chip. Type '@<name>',
    press the menu item, verify the chip is in the field. False -> field cleared,
    caller falls back to the old text trigger."""
    global _ENTRY_CACHE
    try:
        _activate(wid)
        entry = find_real_entry()
        _click_entry(entry)
        sh(["xdotool", "key", "--clearmodifiers", "ctrl+a"])
        sh(["xdotool", "key", "--clearmodifiers", "Delete"])
        sh(["xdotool", "type", "--clearmodifiers", "--delay", "40", "@" + name[:9]])
        t0, item = time.time(), None
        while time.time() - t0 < float(os.environ.get("GEMINI_SKILL_MENU_S", "6")):
            item = _find_menu_item(name)
            if item is not None:
                break
            time.sleep(0.4)
        if item is None:
            print("[skill] menu item %s not found - text trigger fallback" % name, file=sys.stderr)
        else:
            item.queryAction().doAction(0)
            time.sleep(0.6)
            if name in _field_text(entry):
                _ENTRY_CACHE = entry
                print("[skill] chip %s attached in %.1fs" % (name, time.time() - t0), file=sys.stderr)
                return True
            print("[skill] chip not in the field after press - text trigger fallback", file=sys.stderr)
        sh(["xdotool", "key", "--clearmodifiers", "ctrl+a"])
        sh(["xdotool", "key", "--clearmodifiers", "Delete"])
    except Exception as exc:
        print("[skill] attach failed: %s - text trigger fallback" % exc, file=sys.stderr)
    return False


def read_response(timeout=40, poll=0.6, min_requests=1):
    import pyatspi

    def find_app(name):
        desktop = pyatspi.Registry.getDesktop(0)
        for app in desktop:
            if app.name == name:
                return app
        return None

    ans_nodes = {}  # [claude] position in `out` -> answer-heading node (Markdown)

    def walk(node, out, btns=None):
        try:
            role = node.getRoleName()
            iface = node.get_interfaces()
        except Exception:
            return
        if btns is not None and role in ("push button", "toggle button"):  # [claude] rating buttons are toggles
            try:
                btns.append(node.name or "")
            except Exception:
                pass
        consumed = set()
        if "Text" in iface:
            if role in ("paragraph", "list item"):
                t, consumed = _inline_text(node)
            else:
                t = _atspi_text(node).replace("\ufffc", "")
            if t and len(t.strip()) > 2:
                out.append((role, t))
                if role == "heading" and any(t.strip().startswith(h) for h in UI_ANSWER_HEADING):
                    ans_nodes[len(out) - 1] = node
            elif t and t.strip() and role in ("paragraph", "static"):
                # 1-2 char text (answers like "ok", "42"): kept under a separate role so the
                # normal parsing is unchanged; used only as a fallback when no normal block was found
                out.append(("short:" + role, t))
        try:
            n = node.childCount
        except Exception:
            n = 0
        for i in range(n):
            if i in consumed:
                continue
            try:
                child = node.getChildAtIndex(i)
            except Exception:
                continue
            walk(child, out, btns)

    md_node = None
    deadline = time.time() + timeout
    last_len = -1
    stable_count = 0
    result_blocks = []
    while time.time() < deadline:
        app = find_app("Firefox")
        if app is None:
            time.sleep(poll)
            continue
        out = []
        btns = []
        ans_nodes.clear()
        walk(app, out, btns)
        # The block right after the request heading is an ECHO of what WE sent
        # (Gemini's own UI shows your submitted message there), not the
        # answer. The real answer only starts after the LATER answer heading.
        # Collecting from the request heading onward (old code) meant an
        # unfinished/slow-to-render answer read back as our own echoed
        # prompt -- confirmed live 2026-09-29 (identical 3092-char capture
        # matching the outgoing prompt, twice, on requests that never got a
        # real reply in time).
        req_idx = None
        n_requests = 0
        for i, (role, t) in enumerate(out):
            if role == "heading" and any(t.strip().startswith(h) for h in UI_REQUEST_HEADING):
                req_idx = i
                n_requests += 1
        # Only read once OUR request is on the page. Right after paste+Enter
        # the page can still show just the activation turn; reading then
        # returned the ACTIVATION's answer as if it were the task's answer
        # ("I am ready to help format tool calls..."), and on turn 1 an empty
        # read -- confirmed live 2026-09-30.
        if n_requests < min_requests:
            time.sleep(poll)
            continue
        ans_idx = None
        if req_idx is not None:
            for i in range(req_idx + 1, len(out)):
                role, t = out[i]
                if role == "heading" and any(h in t for h in UI_ANSWER_HEADING):
                    ans_idx = i
                    break
        if ans_idx is not None:
            md_node = ans_nodes.get(ans_idx)
        blocks = []
        if ans_idx is not None:
            for role, t in out[ans_idx + 1:]:
                if role in ("paragraph", "static", "heading"):
                    if role == "heading":
                        continue
                    ts = t.strip()
                    if ts.startswith("Gemini") and any(d in ts for d in UI_DISCLAIMER):
                        continue  # fixed UI disclaimer, never part of the answer
                    blocks.append(ts)
                if role == "entry":
                    break
        if not blocks and ans_idx is not None:
            for role, t in out[ans_idx + 1:]:
                if role == "entry":
                    break
                if role.startswith("short:") and any(ch.isalnum() for ch in t):
                    blocks.append(t.strip())
        text_now = "\n".join(blocks)
        # [claude] 2026-11-14 PERF: Gemini UI done-signal instead of waiting for
        # 2 more equal reads (~3s). While generating, the voice-input button is
        # gone; when the answer is complete it is back and every finished answer
        # gets a 'Good response' rating button (verified live). Stability check
        # below stays as the fallback.
        if text_now and n_requests >= min_requests:
            _voice = any(any(b.startswith(v) for v in UI_VOICE_BUTTON) for b in btns)
            _rated = sum(1 for b in btns if b in UI_GOOD_RESPONSE)
            if _voice and _rated >= n_requests:
                result_blocks = blocks
                break
        if text_now and len(text_now) == last_len:
            stable_count += 1
            if stable_count >= 2:
                result_blocks = blocks
                break
        else:
            stable_count = 0
            last_len = len(text_now)
            result_blocks = blocks
        time.sleep(poll)
    # [claude] 2026-11-14: rebuild the final answer as Markdown from the page
    # structure (headings, lists, code fences, tables, bold) - flat text is the fallback.
    if result_blocks and md_node is not None:
        try:
            import gemini_markdown
            md = gemini_markdown.response_markdown(md_node)
            if md:
                return md
        except Exception as _md_exc:
            print("[markdown] failed: %s" % _md_exc, file=sys.stderr)
    return "\n".join(result_blocks).strip()


def _pids_matching(pattern):
    """pgrep -f wrapper. NEVER use pkill -f here: pkill's own argv contains
    the pattern text, so pkill -f can match and SIGKILL/SIGTERM its own
    invoking shell (self-kill pitfall, hit in production 2026-09-29).
    pgrep + explicit os.kill(pid) avoids that entirely."""
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [int(p) for p in r.stdout.split() if p.strip()]


def teardown():
    """Kill the Gemini Firefox + its Xvfb to free resources. Never touches
    DISPLAY :1 / VK's processes — filters explicitly by DISPLAY :2 owner
    and by the gemini-pump profile path."""
    r = sh(["xdotool", "search", "--class", "firefox"])
    for wid in r.stdout.split():
        sh(["xdotool", "windowkill", wid])
    time.sleep(1)
    for pid in _pids_matching(f"firefox-esr --profile {PROFILE}"):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(1)
    for pid in _pids_matching(f"fluxbox -display {DISPLAY}"):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    # SIGKILL never gives Firefox a chance to remove its own profile lock,
    # so the next start_firefox() fails with 'Gemini window did not appear'
    # -- confirmed live 2026-09-30. Clean it here, every time.
    for fn in ('lock', '.parentlock'):
        try:
            os.remove(os.path.join(PROFILE, fn))
        except FileNotFoundError:
            pass
        except Exception:
            pass
    for pid in _pids_matching(f"Xvfb {DISPLAY} -screen"):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    try:
        os.remove(XVFB_LOCK)
    except FileNotFoundError:
        pass


BRIDGE_LOCK = "/tmp/gemini_atspi_bridge.lock"


def attach_images(wid, paths):
    """[claude] 2026-11-12 VISION: paste images into the chat input via the
    clipboard (same proven method as gemini_image_job.py). Every image is
    converted to PNG first (xclip image/png); field is cleared once before."""
    import tempfile
    _activate(wid)
    e = find_real_entry()
    if e is not None:
        try:
            e.queryComponent().grabFocus(); time.sleep(0.3)
        except Exception:
            pass
    sh(["xdotool", "key", "--clearmodifiers", "ctrl+a"])
    sh(["xdotool", "key", "--clearmodifiers", "Delete"])
    env = dict(os.environ); env["DISPLAY"] = DISPLAY
    n = 0
    for p in paths:
        png = tempfile.mktemp(suffix=".png", prefix="gvision_")
        r = subprocess.run(["convert", p + "[0]", "-flatten", png], capture_output=True, timeout=30)
        if r.returncode != 0 or not os.path.exists(png):
            print("[vision] convert failed for %s: %s" % (p, r.stderr[:200]), file=sys.stderr)
            continue
        c = subprocess.Popen(["xclip", "-selection", "clipboard", "-t", "image/png", "-i", png],
                             env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.4)
        sh(["xdotool", "key", "--clearmodifiers", "ctrl+v"], timeout=15)
        time.sleep(3)
        c.kill(); c.wait()
        subprocess.run(["pkill", "-u", str(os.getuid()), "-x", "xclip"])
        try: os.unlink(png)
        except OSError: pass
        time.sleep(0.6)
        e = find_real_entry()
        if e is not None:
            try: e.queryComponent().grabFocus(); time.sleep(0.3)
            except Exception: pass
        n += 1
    print("[vision] attached %d/%d image(s)" % (n, len(paths)), file=sys.stderr)
    return n


def _limit_check(ok):
    """Ok from the model guard is not enough - right at the limit the
    picker still reads the target model. The page banner decides (stale one ignored)."""
    import gemini_model, gemini_limit
    if not ok:
        # only a limit of the asked model counts (locked/missing is not one)
        if gemini_model.LAST_FAIL == "limit":
            return ok, gemini_model.LAST_LIMIT
        if gemini_model.LAST_FAIL and gemini_model.spec()[1].get("strict"):
            return ok, ""   # Auto/Neon locked or missing: no park, exit 7 follows
        return ok, gemini_model.limit_info()   # Hydrogen: as in GD2 (page banner)
    lt = gemini_model.limit_banner()
    if gemini_limit.banner_active(lt):
        if gemini_model.spec()[0] != "hydrogen":
            # the banner may be about another model (Hydrogen limited,
            # Lite asked) -> the asked model's own menu item decides
            own = gemini_model.item_limit()
            print("[limit] page says %r, %s item: %r" % (lt, gemini_model.spec()[1]["name"], own),
                  file=sys.stderr)
            if not own:
                return True, ""
            lt = own
        print("[limit] picker shows the target but the page says: %s" % lt, file=sys.stderr)
        gemini_model.LAST_LIMIT = lt
        return False, lt
    return True, ""


def _model_unavailable(keep_open):
    """A strict model (Auto, Neon) asked by the request could not be
    selected -> send nothing, exit 7; the proxy answers the client with
    the plain reason instead of silently using another model."""
    import gemini_model
    sp = gemini_model.spec()[1]
    if gemini_model.LAST_FAIL == "limit":
        why = "%s: usage limit reached (%s)" % (sp["name"], gemini_model.LAST_LIMIT)
    elif gemini_model.LAST_FAIL == "picker":
        why = "%s: could not select the model (model menu not found)" % sp["name"]
    else:
        why = gemini_model.unavailable_text()
    print("[model] UNAVAILABLE: %s" % why, file=sys.stderr)
    if not keep_open:
        teardown()
    sys.exit(7)


def main():
    # One bridge run at a time, system-wide. Two concurrent runs share the
    # same :2 display + gemini-pump profile, and each one's teardown() kills
    # the other's Xvfb/Firefox mid-request -- confirmed live 2026-09-30 as
    # the cause of 'Exiting due to channel error' / 'cannot open display :2'
    # / 'Gemini window did not appear' (an orphaned delegate session kept
    # firing requests alongside new runs). Blocking flock: a second run
    # waits its turn instead of destroying the first.
    _lock_fd = open(BRIDGE_LOCK, "w")
    fcntl.flock(_lock_fd, fcntl.LOCK_EX)
    args = sys.argv[1:]
    keep_open = "--keep-open" in args
    args = [a for a in args if a != "--keep-open"]
    timeout = 40
    if "--timeout" in args:
        i = args.index("--timeout")
        timeout = int(args[i + 1])
        del args[i:i + 2]
    continue_conv = None
    if "--continue" in args:
        i = args.index("--continue")
        continue_conv = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    # Read the prompt from stdin: a large Hermes context does not fit in argv
    # (exec limit ~2MB, OSError: Argument list too long — broke the whole session
    # c809c5 2026-10-02). With --stdin, argv[1] is interpreted as the activation
    # and the prompt itself is read from stdin until EOF.
    images = []  # [claude] 2026-11-12 vision: --images a.png,b.jpg
    if "--images" in args:
        i = args.index("--images")
        images = [x for x in (args[i + 1] if i + 1 < len(args) else "").split(",") if x]
        del args[i:i + 2]
        continue_conv = None  # vision = always a fresh, isolated chat
    # --limit-wait = agent turn in wait mode: if the target
    # model is limited, send NOTHING and exit 5 (the proxy parks the turn).
    # --probe = wake-up check only: model guard, state file, exit 0 ok / 5 limited / 6 unknown.
    limit_wait = "--limit-wait" in args
    probe = "--probe" in args
    args = [a for a in args if a not in ("--limit-wait", "--probe")]
    if probe:
        ensure_running()
        import gemini_model
        _ok, _lt = _limit_check(gemini_model.ensure_model())
        gemini_model.write_state(_ok)
        if _ok:
            print("[limit] PROBE OK", file=sys.stderr)
            code = 0
        else:
            print("[limit] LIMITED: %s" % _lt if _lt else "[limit] PROBE UNKNOWN (model %r)"
                  % gemini_model.current_model(), file=sys.stderr)
            code = 5 if _lt else 6
        if not keep_open:
            teardown()
        sys.exit(code)
    read_prompt_stdin = "--stdin" in args
    args = [a for a in args if a != "--stdin"]
    if len(args) < (1 if read_prompt_stdin else 2):
        print("usage: gemini_atspi_bridge.py \"activation\" \"prompt\" [--keep-open] [--timeout N] [--continue conv_id] [--stdin]", file=sys.stderr)
        sys.exit(2)
    activation = args[0]
    prompt = args[1] if len(args) > 1 else ""
    if read_prompt_stdin:
        prompt = sys.stdin.read()
        print("[bridge] prompt read from stdin (%d chars)" % len(prompt), file=sys.stderr)

    activation_merged = False
    wid = ensure_running()
    if continue_conv:
        # Continue an existing session: do NOT create a new chat, open the saved
        # conv_id by URL. Gemini restores exactly that session.
        # the chat is already on screen -> no reload (saves the page load and
        # re-render; GEMINI_ALWAYS_NAVIGATE=1 = old behaviour)
        _cur = None
        try:
            _cur = get_current_conv_id()
        except Exception:
            pass
        if (os.environ.get("GEMINI_ALWAYS_NAVIGATE", "0") != "1" and _cur
                and continue_conv.split("/")[-1] in _cur):
            print("[navigate] already open: %s - no reload" % continue_conv, file=sys.stderr)
        else:
            navigate_to_chat(continue_conv)
            time.sleep(1.0)
        # If the saved chat did not open we must NOT paste
        # into whatever chat is on screen. Exit 4 -> proxy rebinds to a fresh chat.
        if os.environ.get("GEMINI_DELTA", "1") != "0" and not conv_is_open(continue_conv):
            print("[navigate] LOST: %s not open, nothing sent (exit 4)" % continue_conv,
                  file=sys.stderr)
            if not keep_open:
                teardown()
            sys.exit(4)
    else:
        # New task: a fresh chat, isolated from the previous context.
        new_chat_session(wid)
    # Gemini resets the model now and then -> check and switch to
    # GEMINI_WEB_MODEL on EVERY session, fresh and continue, before the skill
    # inject. Finds the picker by name, no coordinates. The target comes per
    # request (set by the proxy from the client's model id); a strict model that
    # cannot be selected -> exit 7, a limited one in wait mode -> exit 5 (park).
    # MODEL CHECK CACHE: the model guard walks the whole page (~1-3 s). On a
    # continued chat whose model + level were verified less than
    # GEMINI_MODEL_CHECK_TTL s ago (default 120) it is skipped. Fresh chats, a
    # model/level change or an expired entry always check. The level guard is
    # cheap (~0.3 s) and always runs.
    _mc_key = "%s|%s|%s" % (continue_conv or "", os.environ.get("GEMINI_WEB_MODEL", ""),
                            os.environ.get("GEMINI_WEB_EFFORT", ""))
    _mc_skip = False
    if continue_conv:
        try:
            _mc = json.load(open(_MODEL_CHECK_FILE, encoding="utf-8"))
            _mc_skip = (_mc.get("key") == _mc_key and
                        time.time() - float(_mc.get("ts", 0)) < float(os.environ.get("GEMINI_MODEL_CHECK_TTL", "120")))
        except Exception:
            _mc_skip = False
    if _mc_skip:
        print("[model] check skipped (same chat/model/level verified recently)", file=sys.stderr)
    else:
        try:
            import gemini_model
            _ok = gemini_model.ensure_model()
            _lt = ""
            if limit_wait:
                _ok, _lt = _limit_check(_ok)
            if not _ok and limit_wait and _lt:
                gemini_model.write_state(_ok)
                print("[limit] LIMITED: %s" % _lt, file=sys.stderr)
                if not keep_open:
                    teardown()
                sys.exit(5)
            if not _ok and gemini_model.spec()[1].get("strict"):
                _model_unavailable(keep_open)
            gemini_model.write_state(_ok)
        except Exception as _sel_exc:
            print("[model] guard failed: %s - continuing with current model" % _sel_exc, file=sys.stderr)
        if continue_conv:
            try:
                import gemini_model as _gm
                if _gm.LAST_FAIL == "":
                    json.dump({"key": _mc_key, "ts": time.time()}, open(_MODEL_CHECK_FILE, "w"))
            except Exception:
                pass
    # Gemini resets the reasoning level to low now and then -> read it and set
    # GEMINI_WEB_EFFORT (default medium) every session.
    try:
        import gemini_model
        _ef = gemini_model.ensure_effort()
        print("[effort] %s -> %s (target %s, %.1f s)" % (_ef['before'], _ef['after'],
              _ef['target'], _ef['secs']), file=sys.stderr)
    except Exception as _ef_exc:
        print("[effort] guard failed: %s - continuing" % _ef_exc, file=sys.stderr)
    # SKILL CHIP (see attach_skill): the skill rides with THIS message as a chip -
    # no text trigger, no separate activation turn (fresh chats save ~12 s).
    # GEMINI_SKILL_MODE=text = old behaviour; failure -> old behaviour.
    _chip = False
    _chip_turn = False
    # GEMINI_SKILL_MODE: chip (default) | text (old "/skill" text trigger) | none (no
    # skill at all - the call format lives in the prompt). File skill_mode next to
    # this script overrides the env (switch without a proxy restart).
    _skill_mode = os.environ.get("GEMINI_SKILL_MODE", "chip")
    try:
        _smf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skill_mode")
        if os.path.exists(_smf):
            _skill_mode = open(_smf).read().strip() or _skill_mode
    except Exception:
        pass
    if activation and _skill_mode == "none":
        print("[skill] mode none: no skill trigger", file=sys.stderr)
        activation = ""
        activation_merged = True
    if activation and not images and _skill_mode != "text":
        _skill = activation.lstrip("/").split()[0]
        _chip = attach_skill(wid, _skill)
        if _chip and not continue_conv and os.environ.get("GEMINI_CHIP_ACTIVATION_TURN", "1") != "0":
            # on a FRESH chat the skill gets its own short turn
            # first (seen live again: chip + a 60k Hermes prompt in one message ->
            # "tool call limit 0"; the separate activation turn cured that before).
            try:
                sh(["xdotool", "key", "--clearmodifiers", "End"])
                sh(["xdotool", "type", "--clearmodifiers", "--delay", "10",
                    os.environ.get("GEMINI_CHIP_ACTIVATION_TEXT", "Activate this skill. Reply with one word: ready.")])
                time.sleep(0.3)
                sh(["xdotool", "key", "--clearmodifiers", "Return"])
                _t_ack = time.time()
                time.sleep(0.5)
                wait_idle(min_rated=1, timeout=45)
                print("[skill] activation turn done in %.1fs" % (time.time() - _t_ack), file=sys.stderr)
                _chip_turn = True
                _chip = attach_skill(wid, _skill)  # chip on the task message too
            except Exception as _ce:
                print("[skill] activation turn failed: %s" % _ce, file=sys.stderr)
        if _chip or _chip_turn:
            activation_merged = True
            activation = ""
    if activation:
        if continue_conv:
            # CONTINUE: the inject goes TOGETHER with the prompt, as one message
            # (user requirement 2026-10-02: "the inject phrase arrives as one
            # separate message - it must go together with the hermes prompt").
            # A separate trigger on continue was an extra turn and shifted the
            # model to an ack instead of the task. We prefix the activation onto
            # the body — the skill stays active, but without a separate message
            # and without waiting for an ack.
            # SEPARATOR `>>>` instead of "\n\n" (FIX 2026-10-03): a live-window
            # measurement showed contenteditable DROPS \n on paste — of 69 chars
            # only 67 arrived, and "skill tool-call-formatter" stuck to the task
            # as one word with no boundary (verified in the AT-SPI tree:
            # 'skill tool-call-formatterWhat city now?'). Candidate run:
            # "\n\n" -> OK=False, " " -> OK=True, " >>>" -> OK=True, "\n>>>" -> OK=False.
            prompt = activation + " >>> " + prompt if activation else prompt
        elif os.environ.get("GEMINI_SEPARATE_ACTIVATION", "1") == "0":
            # [claude] 2026-11-14: merged activation (one message) is OFF by default -
            # verified live: on big Hermes prompts (~60k) the skill did not wake up and
            # Gemini claimed "tool calls limit 0"; with the separate activation turn the
            # same task called terminal at once. Opt-in only: GEMINI_SEPARATE_ACTIVATION=0.
            prompt = activation + " >>> " + prompt
            activation_merged = True
        else:
            # FRESH (first inject): as a separate message with an ack pause, so
            # the skill really activates before the main request (the first time
            # the skill is not active yet, it must be raised separately).
            send_prompt(wid, activation)
            # We do NOT wait for the ack with a full read_response (up to 25s +
            # stabilization x2 — the main thief of the first timings). We need
            # NOT the answer, only for the skill to have time to activate. A
            # short fixed pause (~3s). PERF 2026-10-03: reduced 0.8->0.5s and
            # 4 attempts->3: before, waiting for activation ate up to 3.2+3s of
            # read_response. 1.5s of pause is enough — the ack is only needed as
            # "the skill woke up", not as a result; if no ack came, we go on
            # anyway (fail-open).
            # [claude] 2026-11-14: wait for the REAL end of the ack (UI done-signal),
            # not a fixed ~7 s - Hydrogen sometimes thinks longer, the task was then
            # pasted while the ack was still generating, Enter got ignored and the
            # proxy waited 120 s for nothing ("empty read" + full resend).
            time.sleep(0.5)
            _t_ack = time.time()
            wait_idle(min_rated=1, timeout=45)
            _ack = read_response(timeout=3, min_requests=1)
            print("[activation] ack done in %.1fs" % (time.time() - _t_ack), file=sys.stderr)
            print("[activation ack, %d chars]: %s" % (len(_ack), _ack[:200]), file=sys.stderr)
            try:
                with open("/tmp/gemini_debug_activation.txt", "w", encoding="utf-8") as _f:
                    _f.write("SENT: %r\nACK (%d chars):\n%s\n" % (activation, len(_ack), _ack))
            except Exception:
                pass
    # Step 2 of 2: the real request, as a continuation of the same chat.
    try:
        _prev_sig = last_request_signature()
    except Exception as _se:
        print("[send] signature failed: %s - no confirm" % _se, file=sys.stderr)
        _prev_sig = None
    if images:
        if not attach_images(wid, images):
            print("[vision] no image could be attached", file=sys.stderr)
            sys.exit(3)
        _pasted = send_prompt(wid, prompt, clear=False) or True  # vision: images count too
    else:
        _pasted = send_prompt(wid, prompt, clear=not _chip)  # keep the skill chip
    if not _pasted or not confirm_sent(_prev_sig):
        # never read the OLD answer back as the new one:
        # continue -> exit 4 (proxy opens a fresh chat with the full prompt),
        # fresh chat -> exit 9 (plain error to the client).
        _code = 4 if continue_conv else 9
        print("[send] NOT ACCEPTED by Gemini (page unchanged) - exit %d" % _code, file=sys.stderr)
        if not keep_open:
            teardown()
        sys.exit(_code)
    # Seen live: an AT-SPI call inside read_response can block forever (the deadline
    # is only checked between tree walks) and the bridge hangs until the proxy kills
    # it. Hard watchdog: past timeout + 45 s the bridge exits by itself - 4 on
    # continue (the proxy reopens the task in a fresh chat), 9 on a fresh chat.
    import threading as _th
    def _read_watchdog():
        sys.stderr.write("[read] WATCHDOG: read_response stuck > %ds - exit\n" % (int(timeout) + 45))
        sys.stderr.flush()
        os._exit(4 if continue_conv else 9)
    _wd = _th.Timer(int(timeout) + 45, _read_watchdog)
    _wd.daemon = True
    _wd.start()
    response = read_response(timeout=timeout, min_requests=2 if (activation and not continue_conv and not activation_merged) or _chip_turn else 1)
    _wd.cancel()
    print(response.replace("\ufffc", ""))

    # Save the conv_id AFTER a real message has been sent: a freshly-created
    # chat has NO id in the address bar yet (gemini.google.com/app without
    # suffix — confirmed live 2026-09-30), so reading before send_prompt
    # returned None. Only after the first turn Gemini assigns /app/<conv_id>;
    # then the server can bind it to the Hermes session_id and later re-open
    # exactly this conversation via --continue.
    if not continue_conv:
        try:
            _conv_url = get_current_conv_id()
            if _conv_url:
                with open("/tmp/gemini_last_conv_id.txt", "w", encoding="utf-8") as _f:
                    _f.write(_conv_url)
                print("[new chat conv_id] %s" % _conv_url, file=sys.stderr)
            else:
                print("[new chat conv_id] None (id not yet assigned?)", file=sys.stderr)
        except Exception as _e:
            print("[conv_id read failed] %s" % _e, file=sys.stderr)

    if not keep_open:
        teardown()


if __name__ == "__main__":
    main()
