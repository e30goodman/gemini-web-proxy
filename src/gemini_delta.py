"""DELTA mode: on continue send Gemini only the NEW messages.

The Gemini chat already holds everything sent earlier, so re-pasting the whole
Hermes history every agent step (65-75 KB, ~2 min/step, chat full of
duplicates) is pure waste. Per Hermes session we remember how many non-system
messages were already delivered and a fingerprint of that prefix. Next step:
if the prefix still matches, only the tail goes out (minus Gemini's own
answer, which is already in the chat).

Fingerprint: role sequence + content of user/tool messages. Assistant messages
count by role only - Hermes re-renders their text (tool_calls JSON etc.), so
their text is not stable.

Pure logic here (no I/O besides the state file and the log), unit test:
(offline tests kept privately).
"""

import hashlib
import json
import os
import time
from pathlib import Path

DELTA_ENABLED = os.environ.get("GEMINI_DELTA", "1") != "0"
DELTA_ROLLOVER = int(os.environ.get("GEMINI_DELTA_ROLLOVER", "40"))
_VAR = Path(__file__).parent / "var"
DELTA_STATE_FILE = os.environ.get("GEMINI_DELTA_STATE", str(_VAR / "gemini_delta_state.json"))
DELTA_LOG = os.environ.get("GEMINI_DELTA_LOG", str(_VAR / "delta.log"))
DELTA_PRUNE_S = 7 * 86400
# 1 = every delta carries the tool schemas (default). 0 = schemas only when the
# tool set changed since the last delivery (they are already in the chat).
DELTA_SCHEMAS_ALWAYS = os.environ.get("GEMINI_DELTA_SCHEMAS", "1") != "0"
TOOLS_SAME_NOTE = "(Tool list unchanged - use the tools and call format given earlier in this chat.)"

DELTA_HEADER = ("[CONTINUATION of the same task. Only NEW messages since your last "
                "answer are below; the earlier context is above in this chat.]")


def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
    return str(content)


def non_system(messages):
    return [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]


def msg_hash(m) -> str:
    role = m.get("role") or ""
    body = "" if role == "assistant" else _text(m.get("content"))
    return hashlib.sha1((role + "\x00" + body).encode("utf-8", "replace")).hexdigest()[:10]


def hashes(msgs):
    return [msg_hash(m) for m in msgs]


def tools_hash(tools) -> str:
    return hashlib.sha1(json.dumps(tools or [], sort_keys=True, ensure_ascii=False)
                        .encode("utf-8", "replace")).hexdigest()[:10]


def pick(nonsys, state, bound_url, rollover=DELTA_ROLLOVER):
    """Decide what to send. Returns (mode, tail, reason).

    mode: "FULL"    - no bound chat (fresh) or bound chat without state
                      (old behaviour: whole history into the bound chat);
          "DELTA"   - tail = only the new messages;
          "RESTART" - history changed / rollover: caller unbinds and sends a
                      fresh chat with the full prompt.
    """
    if not bound_url:
        return "FULL", None, "fresh"
    if not state or state.get("conv") != bound_url:
        return "FULL", None, "nostate"
    n = int(state.get("n") or 0)
    if len(nonsys) < n or hashes(nonsys[:n]) != list(state.get("hs") or []):
        return "RESTART", None, "history-changed"
    if int(state.get("steps") or 0) >= rollover:
        return "RESTART", None, "rollover"
    tail = list(nonsys[n:])
    while tail and tail[0].get("role") == "assistant":
        tail.pop(0)
    reason = "new"
    if not tail:
        # same request repeated (client retry): resend what follows the last
        # assistant message
        last_a = max((i for i, m in enumerate(nonsys) if m.get("role") == "assistant"),
                     default=-1)
        tail = list(nonsys[last_a + 1:])
        reason = "repeat"
    if not tail:
        return "FULL", None, "empty-tail"
    return "DELTA", tail, reason


def record(state_db, sid, nonsys, conv_url, continued, th=""):
    prev = state_db.get(sid) or {}
    steps = int(prev.get("steps") or 0) + 1 if (continued and prev.get("conv") == conv_url) else 0
    state_db[sid] = {"n": len(nonsys), "hs": hashes(nonsys), "conv": conv_url,
                     "steps": steps, "th": th, "ts": time.time()}
    return state_db[sid]


def prune(state_db, now=None):
    now = now or time.time()
    for k in [k for k, v in state_db.items()
              if now - float((v or {}).get("ts") or 0) > DELTA_PRUNE_S]:
        del state_db[k]


def load_state(path=None) -> dict:
    try:
        with open(path or DELTA_STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(state_db, path=None) -> None:
    path = path or DELTA_STATE_FILE
    try:
        prune(state_db)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state_db, f)
        os.replace(tmp, path)
    except Exception:
        pass


def log_line(kind, sid, **kw) -> None:
    try:
        os.makedirs(os.path.dirname(DELTA_LOG) or ".", exist_ok=True)
        extra = " ".join("%s=%s" % (k, v) for k, v in kw.items())
        with open(DELTA_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s sid=%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), kind, sid, extra))
    except Exception:
        pass
