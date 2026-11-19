#!/usr/bin/python3
"""
gemini_dom_watcher.py — background daemon that continuously scrapes the Gemini window.

Keeps Xvfb :2 + Firefox gemini-pump.default open PERMANENTLY (no teardown per
call — that was the main source of latency: a cold browser start on every
request). Every POLL_SEC it reads the AT-SPI tree and ACCUMULATES the answer
into a buffer file with dedup: it writes only the fragments not yet in the
buffer.

Buffer: Path(__file__).parent / "var" / "dom_buffer.json"
Structure: { conv_id: {"text": "<accumulated answer>", "blocks": [lines],
                        "seen_hashes": {hash: 1}, "updated": epoch, "active": bool} }

Two-layer dedup:
  1) prefix — if the current text starts with what is already written, append
     only the suffix (Gemini renders the answer line by line / as it generates);
  2) block hash — each line block is hashed and not written twice
     (protection against redraw/duplication by the tree).

The daemon keeps the browser via ensure_running() from gemini_atspi_bridge (shared
Xvfb/WM/Firefox logic, single source of truth). Runs strictly under the system
/usr/bin/python3 (which has pyatspi), like the bridge.

Control: SIGTERM -> graceful stop (we do NOT kill Xvfb/Firefox — the daemon is
down, let teardown close the browser on the next request, or systemd Restart).
Liveness check: an updated dom_buffer.json + the PID file.
"""
import os
import sys
import time
import json
import signal
import hashlib
import fcntl
from pathlib import Path

# Same hard interpreter guard as in the bridge — pyatspi only in the system python.
_OK_INTERPRETERS = {"/usr/bin/python3", "/usr/bin/python3.14"}
if os.path.realpath(sys.executable) not in _OK_INTERPRETERS:
    sys.stderr.write(
        "gemini_dom_watcher.py must run under SYSTEM /usr/bin/python3 "
        "(only interpreter with pyatspi). Got: %s\n" % sys.executable
    )
    sys.exit(3)

from gemini_atspi_bridge import (  # noqa: E402  (shared browser logic)
    ensure_running,
    get_current_conv_id,
    find_gemini_window,
    teardown,
    DISPLAY,
    UI_ANSWER_HEADING,
    UI_REQUEST_HEADING,
    UI_DISCLAIMER,
)

POLL_SEC = float(os.environ.get("WATCH_POLL_SEC", "1.5"))
# PERF 2026-10-03: the buffer lived forever. Measurement: 102 records / 4.8 MB,
# and save_buffer rewrites ALL 4.8 MB on every tick (23 times in 10 min in
# journalctl) — i.e. an hour of CPU+disk just rewriting dead chats. We keep only
# the active chat + recent ones; old ones are dropped (their answers are needed
# by nobody anyway: a session can only be continued via _session_db, which stores
# the URL, not the buffer text).
BUFFER_MAX_CONVS = int(os.environ.get("WATCH_BUFFER_MAX_CONVS", "8"))
BUFFER_MAX_AGE_S = float(os.environ.get("WATCH_BUFFER_MAX_AGE_S", "7200"))  # 2h
BUFFER_KEEP_ACTIVE = 3   # active + 2 newer than it
# Minimum interval between full buffer rewrites. The server reads the answer via
# _read_buffer_answer several times per turn, but emits one final text — writing
# more often than SAVE_MIN_GAP_S is pointless.
SAVE_MIN_GAP_S = float(os.environ.get("WATCH_SAVE_MIN_GAP_S", "2.0"))
BUFFER_FILE = Path(os.environ.get(
    "WATCH_BUFFER_FILE", str(Path(__file__).parent / "var" / "dom_buffer.json")
))
PID_FILE = Path("/tmp/gemini_dom_watcher.pid")
LOCK_FILE = Path("/tmp/gemini_dom_watcher.lock")

# --- Auto-teardown of the browser on idle ---------------------------------
# Before, Firefox lived forever: the server always passes --keep-open to the
# bridge while the daemon is alive (see server.py `_watcher_alive`), and the
# daemon by design does NOT close the browser ("let teardown close it on the next
# request"). But the next request may not come for hours — in the 2026-10-03
# measurement Firefox with the gemini-pump profile sat idle 7.7 hours after the
# last real request, using ~700 MB RSS.
#
# Now the daemon closes the browser ITSELF after IDLE_TIMEOUT_S of idle. Closing
# immediately is wrong: peak memory is between requests, and teardown() cold-
# starts the browser again on the next request. So we wait IDLE_TIMEOUT_S and do
# NOT touch the browser if the buffer shows an active generation.
IDLE_TIMEOUT_S = float(os.environ.get("WATCH_IDLE_TIMEOUT_S", "900"))  # 15 min
# Shorter idle at night: 00:00-07:00 local time the browser closes after
# WATCH_IDLE_NIGHT_S (default 15 min), daytime keeps WATCH_IDLE_TIMEOUT_S.
# Night window: WATCH_NIGHT_FROM / WATCH_NIGHT_TO (hours).
IDLE_NIGHT_S = float(os.environ.get("WATCH_IDLE_NIGHT_S", "900"))
NIGHT_FROM = int(os.environ.get("WATCH_NIGHT_FROM", "0"))
NIGHT_TO = int(os.environ.get("WATCH_NIGHT_TO", "7"))


def _idle_limit() -> float:
    h = time.localtime().tm_hour
    night = (NIGHT_FROM <= h < NIGHT_TO) if NIGHT_FROM <= NIGHT_TO else (h >= NIGHT_FROM or h < NIGHT_TO)
    return IDLE_NIGHT_S if night else IDLE_TIMEOUT_S
# After our own teardown, do not bring the browser back via self-heal for a while,
# otherwise teardown <-> ensure_running fight in an endless loop (2026-10-04).
RECOVERY_SUPPRESS_S = float(os.environ.get("WATCH_RECOVERY_SUPPRESS_S", "420"))  # 7 min
# A generation is considered active if it updated recently: the Gemini answer is
# appended in blocks, and there are pauses of tens of seconds between blocks.
ACTIVE_GENERATION_GRACE_S = float(os.environ.get("WATCH_ACTIVE_GRACE_S", "120"))


def _bridge_busy() -> bool:
    """Is gemini_atspi_bridge.py alive right now (a one-shot server request)?

    The daemon and the server share one browser. If, at the moment of the idle
    decision, the server has already started the bridge, teardown() would cut the
    generation in half.

    IMPORTANT: do not use `pgrep -f gemini_atspi_bridge.py` — the substring also
    matches the COMMAND LINE of pgrep itself (the shell that invoked it) and any
    script mentioning the name. Verified live 2026-10-03: pgrep returned changing
    PIDs that were actually bash wrappers, and there was no bridge at all — with
    such a check teardown would never fire. So we scan /proc and verify that
    argv[0] is the python interpreter and argv[1] is the bridge script itself.
    """
    try:
        me = os.getpid()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == me:
                continue
            try:
                with open("/proc/%d/cmdline" % pid, "rb") as f:
                    argv = f.read().split(b"\0")
            except (OSError, IOError):
                continue
            argv = [a.decode("utf-8", "replace") for a in argv if a]
            if len(argv) < 2:
                continue
            if not os.path.basename(argv[0]).startswith("python"):
                continue
            if os.path.basename(argv[1]) != "gemini_atspi_bridge.py":
                continue
            return True
        return False
    except Exception:
        # Could not check — treat the bridge as busy: better not to close the
        # browser for nothing (an extra cold start) than to cut a live answer.
        return True

# --- minimal AT-SPI walk: return all text blocks (role, text) ---
def _walk_text(app, out, heads=None):
    try:
        role = app.getRoleName()
        iface = app.get_interfaces()
    except Exception:
        return
    try:
        if "Text" in iface:
            t = app.queryText().getText(0, -1).strip()
            if t and len(t) > 1:
                out.append((role, t))
                if heads is not None and role == "heading" and any(t.startswith(h) for h in UI_ANSWER_HEADING):
                    heads[len(out) - 1] = app
    except Exception:
        pass
    try:
        n = app.childCount
    except Exception:
        return
    for i in range(n):
        try:
            ch = app.getChildAtIndex(i)
        except Exception:
            continue
        _walk_text(ch, out, heads)


def collect_blocks():
    """Return (app, req_blocks, answer_blocks).

    req_blocks — blocks after the last request heading (request+answer mixed,
    as in the bridge's current read_response).
    answer_blocks — ONLY blocks after the last answer heading (the clean answer,
    without the echo of our request) — the source for the server's buffer.
    """
    import pyatspi
    desktop = pyatspi.Registry.getDesktop(0)
    app = None
    for a in desktop:
        if a.name == "Firefox":
            app = a
            break
    if app is None:
        return None, [], []
    out = []
    heads = {}
    _walk_text(app, out, heads)
    # Last request heading and last answer heading
    req_idx = None
    ans_idx = None
    for i, (role, t) in enumerate(out):
        if role == "heading" and any(t.startswith(h) for h in UI_REQUEST_HEADING):
            req_idx = i
        if role == "heading" and any(t.startswith(h) for h in UI_ANSWER_HEADING):
            ans_idx = i
    req_blocks = []
    if req_idx is not None:
        for role, t in out[req_idx + 1:]:
            if role == "entry":
                break
            if role in ("paragraph", "static", "heading"):
                if any(t.startswith(h) for h in UI_ANSWER_HEADING):
                    continue
                if t.startswith("Gemini") and any(d in t for d in UI_DISCLAIMER):
                    continue
                req_blocks.append(t)
    answer_blocks = []
    if ans_idx is not None:
        for role, t in out[ans_idx + 1:]:
            if role == "entry":
                break
            if role in ("paragraph", "static", "heading"):
                if t.startswith("Gemini") and any(d in t for d in UI_DISCLAIMER):
                    continue
                answer_blocks.append(t)
    # [claude] 2026-11-14: Markdown answer (headings/lists/code/tables/bold) from
    # the page structure; the flat blocks stay as the fallback.
    if ans_idx is not None and ans_idx in heads and answer_blocks:
        try:
            import gemini_markdown
            md = gemini_markdown.response_markdown(heads[ans_idx])
            if md:
                answer_blocks = [md]
        except Exception:
            pass
    return app, req_blocks, answer_blocks


def block_hash(b):
    return hashlib.sha1(b.encode("utf-8", "replace")).hexdigest()[:16]


def load_buffer():
    try:
        with open(BUFFER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def save_buffer(data):
    tmp = str(BUFFER_FILE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, BUFFER_FILE)


def prune_buffer(buf, active_conv=None):
    """Drop dead chats: older than BUFFER_MAX_AGE_S or beyond BUFFER_MAX_CONVS.

    The active chat (the one the window is on right now) and a few recent ones
    ALWAYS stay regardless of age — otherwise we could cut an answer the server
    has not read yet. Fail-safe: on any error return buf as is.
    """
    if not buf:
        return buf
    try:
        now = time.time()
        fresh = sorted(buf.items(), key=lambda kv: (kv[1].get("updated") or 0),
                       reverse=True)
        keep, dropped = [], 0
        for i, (k, v) in enumerate(fresh):
            age = now - (v.get("updated") or 0)
            if k == active_conv or i < BUFFER_KEEP_ACTIVE:
                keep.append(k)
            elif age > BUFFER_MAX_AGE_S or i >= BUFFER_MAX_CONVS:
                dropped += 1
            else:
                keep.append(k)
        if dropped:
            for k, _v in fresh:
                if k not in keep:
                    del buf[k]
            print("[watcher] pruned %d stale convs (kept %d)"
                  % (dropped, len(buf)), flush=True)
    except Exception as e:
        print("[watcher] prune failed: %s" % e, file=sys.stderr, flush=True)
    return buf


def main():
    # One daemon per host (flock), like the bridge with bridge.lock.
    lock_fd = open(str(LOCK_FILE), "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another watcher holds %s — exiting" % LOCK_FILE, file=sys.stderr)
        sys.exit(1)

    running = {"stop": False}

    def _onterm(signum, frame):
        running["stop"] = True

    signal.signal(signal.SIGTERM, _onterm)
    signal.signal(signal.SIGINT, _onterm)

    # Guarantee the browser is alive (ensure_running starts Xvfb+Firefox if needed).
    # START can take ~20-30s (cold Firefox) — the first cycle waits patiently.
    print("[watcher] ensuring Xvfb %s + Firefox gemini-pump ..." % DISPLAY)
    wid = ensure_running()
    print("[watcher] window ready wid=%s, entering poll loop (poll=%.1fs)" % (wid, POLL_SEC))

    buf = load_buffer()
    # Clear stale active flags (the previous daemon may have died mid-generation).
    for k in buf:
        buf[k]["active"] = False
    # PERF 2026-10-03: prune the buffer at start — otherwise the daemon spends the
    # first minutes rewriting 4.8 MB of dead chats (measurement: 102 records / 4.8 MB).
    prune_buffer(buf)
    save_buffer(buf)

    with open(str(PID_FILE), "w") as f:
        f.write(str(os.getpid()))

    last_conv = None
    last_save_t = time.time()
    dirty = False
    last_activity_t = time.time()
    last_teardown_t = 0.0  # when the watcher itself closed the browser on idle (suppress instant recovery)
    while not running["stop"]:
        try:
            # --- Auto-teardown of the browser on idle -------------------------
            # Reset the idle timer when the daemon sees signs of live work: new
            # answer blocks or an active generation in the buffer. Otherwise
            # Firefox would live forever (7.7 h in the 2026-10-03 measurement,
            # ~700 MB RSS).
            now = time.time()
            any_active = any(
                e.get("active") and (now - float(e.get("updated") or 0)) < ACTIVE_GENERATION_GRACE_S
                for e in buf.values()
            )
            idle_for = now - last_activity_t
            if idle_for >= _idle_limit() and not any_active:
                # Race with the server: a request could arrive right between ticks.
                # If the server currently holds a live request (the bridge
                # subprocess), we must not close the browser — it would cut the
                # answer in half. We check BEFORE teardown; if the bridge is
                # present, wait for the next tick.
                if _bridge_busy():
                    last_activity_t = now
                    time.sleep(POLL_SEC)
                    continue
                print("[watcher] idle %.0fs, no active generation — tearing down browser"
                      % idle_for, flush=True)
                try:
                    teardown()
                    last_teardown_t = time.time()
                except Exception as ex:
                    print("[watcher] teardown failed: %s" % ex, file=sys.stderr, flush=True)
                last_activity_t = time.time()
                time.sleep(POLL_SEC)
                continue

            # Browser self-heal: if the Gemini window is gone (Firefox died / was
            # killed by a competing process), ensure_running restarts Xvfb/Firefox.
            # Without this the daemon hangs forever on a dead window, collect_blocks
            # -> empty. Fuse: do not bring the browser back right AFTER our own
            # teardown — otherwise teardown and self-heal fight in an endless loop
            # every 15 min (observed 2026-10-04: "tearing down" -> immediately
            # "window lost — restarting"). The server raises Firefox itself on a
            # request (generate_via_atspi_bridge), so here we wait for idle, not spawn.
            if find_gemini_window()[0] is None:
                if now - last_teardown_t >= RECOVERY_SUPPRESS_S:
                    print("[watcher] Gemini window lost — restarting Xvfb+Firefox ...", flush=True)
                    try:
                        ensure_running()
                    except Exception as ex:
                        print("[watcher] browser restart failed: %s" % ex, file=sys.stderr, flush=True)
                        time.sleep(5)
                        continue
                    print("[watcher] browser restarted", flush=True)

            conv = get_current_conv_id() or last_conv
            if not conv:
                time.sleep(POLL_SEC)
                continue
            last_conv = conv
            app, req_blocks, answer_blocks = collect_blocks()
            if app is None or not req_blocks:
                time.sleep(POLL_SEC)
                continue

            entry = buf.setdefault(conv, {
                "text": "", "blocks": [], "seen_hashes": {}, "updated": 0, "active": False
            })
            changed = False
            # REQUEST CHANGE: the first req_block is the echo of the request (the
            # text we sent). If it differs from what we saw before, this is a NEW
            # request in an existing chat (continue / a repeated Hermes session on
            # the same conv). The old answer from the previous request is invalid:
            # reset it so the server does not return it as a fresh answer (real
            # case 2026-10-02, session 05dd78: the client got the echo of the Hermes
            # system prompt instead of the Gemini answer, because the daemon kept
            # the old answer for the conv).
            cur_query = req_blocks[0] if req_blocks else ""
            last_query = entry.get("last_query")
            if last_query is not None and cur_query and cur_query != last_query:
                entry["answer"] = ""
                entry["seen_answers"] = {}
                entry["last_query"] = cur_query
                changed = True
            elif cur_query and not entry.get("last_query"):
                entry["last_query"] = cur_query
            new_blocks = []
            for b in req_blocks:
                h = block_hash(b)
                if h not in entry["seen_hashes"]:
                    entry["seen_hashes"][h] = 1
                    new_blocks.append(b)

            if new_blocks:
                # Prefix dedup: does the current text already contain part? append the tail.
                cur_text = "\n".join(entry["blocks"])
                new_text = "\n".join(req_blocks)
                if cur_text and new_text.startswith(cur_text) and len(new_text) > len(cur_text):
                    delta = new_text[len(cur_text):].lstrip("\n")
                    if delta:
                        entry["blocks"].append(delta)
                        entry["text"] = new_text
                        changed = True
                elif cur_text != new_text:
                    # Redraw / not a prefix: overwrite fully (the truth is in the buffer now).
                    entry["blocks"] = list(req_blocks)
                    entry["text"] = new_text
                    # seen_hashes already updated above — we will not write it again
                    changed = True

            # Separately — the clean answer (without the echo of our request): take
            # answer_blocks. PITFALL "a cut overwrites the full" (2026-10-02, session
            # cf940c): Gemini renders one answer in pieces/redraws, the watcher writes
            # a snapshot each time (58K full -> 103 cut -> 129 continuation), and the
            # server took the last cut. We keep best_answer = the MAXIMUM by length
            # for the current request; a small cut does not erase it. best_answer is
            # reset on a request change (below).
            answer_txt = "\n".join(answer_blocks).strip()
            answer_h = block_hash(answer_txt)
            # Do NOT write the skill-activation ack as the answer/best_answer: it is
            # "The skill has been successfully..." / "tool-call-formatter skill is
            # active" — a service reply to the inject, longer than a short real answer
            # ("Ottawa") and must NOT be passed off as the result (real case
            # 2026-10-02, best_answer grabbed a 779-char ack instead of "Ottawa" 6
            # chars). We filter out ack-like text.
            _ack_like = any(
                s in answer_txt.lower() for s in (
                    "skill has been successfully", "skill is active", "skill is now active",
                    "tool-call-formatter skill", "now active. i am ready",
                    "ready to help format", "format user requests", "structured json tool calls",
                    "such as for", "described functions", "activated successfully", "skill activated",
                    "the `tool-call-formatter` skill", "is active and ready",
                    "skill is active and ready", "user guide",
                )
            )
            if answer_blocks and answer_txt != entry.get("answer", "") and not _ack_like:
                # Duplicate guard: if the hash of the new answer was already seen as an answer — skip.
                seen_answers = entry.get("seen_answers", {})
                if answer_h not in seen_answers:
                    entry["answer"] = answer_txt
                    entry.setdefault("seen_answers", {})[answer_h] = 1
                    # best_answer: only if the NEW one is longer than the current (and not empty junk).
                    cur_best = entry.get("best_answer") or ""
                    if answer_txt and len(answer_txt) >= len(cur_best):
                        entry["best_answer"] = answer_txt
                    changed = True

            if changed:
                entry["updated"] = time.time()
                entry["active"] = True
                # Live work resets the idle timer: while Gemini is still appending
                # the answer, the browser must not be closed (else we lose an
                # unfinished answer).
                last_activity_t = time.time()
                # PERF 2026-10-03: do not write the buffer on EVERY new block.
                # Measurement: 23 full rewrites of dom_buffer.json in 10 minutes,
                # while the server reads the answer only a couple of times per turn.
                # We accumulate changes and flush no more often than SAVE_MIN_GAP_S
                # (or on a big growth — then the answer is already sizable and the
                # server may be mid-read, so deliver the useful part earlier).
                grew = len(answer_txt) - (len(entry.get("best_answer") or "") - 0)
                now_t = time.time()
                if (now_t - last_save_t) >= SAVE_MIN_GAP_S or grew > 2000:
                    prune_buffer(buf, active_conv=conv)
                    save_buffer(buf)
                    last_save_t = now_t
                    print("[watcher] %s: %d new blocks, answer_len=%d (saved)"
                          % (conv, len(new_blocks), len(answer_txt)), flush=True)
                else:
                    dirty = True
                    print("[watcher] %s: %d new blocks, answer_len=%d (buffered)"
                          % (conv, len(new_blocks), len(answer_txt)), flush=True)
        except Exception as e:
            print("[watcher] error: %s" % e, file=sys.stderr, flush=True)
        # Flush pending changes if they have accumulated longer than the interval —
        # otherwise an answer that arrived "in the buffer" on the last tick would not
        # reach the file until the next change (the server would not see it).
        if dirty and (time.time() - last_save_t) >= SAVE_MIN_GAP_S:
            try:
                save_buffer(buf)
                last_save_t = time.time()
                dirty = False
                print("[watcher] flushed buffered changes", flush=True)
            except Exception as e:
                print("[watcher] flush failed: %s" % e, file=sys.stderr, flush=True)
        time.sleep(POLL_SEC)

    print("[watcher] stopping")
    # Final flush on SIGTERM: do not lose the accumulated answer.
    try:
        if dirty:
            save_buffer(buf)
            print("[watcher] final flush on stop", flush=True)
    except Exception:
        pass
    lock_fd.close()


if __name__ == "__main__":
    main()