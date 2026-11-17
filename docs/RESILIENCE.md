# Resilience layers (off by default)

Gemini Web, driven through a browser, has failure modes that a normal API does
not. The proxy ships several **guard dogs** — each is a switch, not a fix-all:

## Rate limiter + circuit breaker (always on)

Protects the Google account from "unusual traffic" **bans**. Sliding windows per
minute and per hour (`GEMINI_RATE_MAX_PER_MIN`, `GEMINI_RATE_MAX_PER_HOUR`), a
min gap between requests, and a cooldown after an upstream 429. Exceeding →
the request waits in a queue up to `GEMINI_RATE_QUEUE_MAX_WAIT_S`, then a clean
503. Without this, load-testing or a busy bot gets the account banned.

## Retry with a fresh session (always on)

On stream loss / empty response / upstream error: close the broken client, open
a fresh one, resend (bounded). An overall request deadline prevents a stalled
upstream from hanging the caller, and a disconnect check stops the proxy from
burning Google quota for a client that already left.

## Judge hook (`JUDGE_ENABLED`)

When a task carried `tools` but Gemini answered with text **and no** tool_call,
a cheap free model decides CONTINUE (send a short nudge) or DONE (let it go).
The nudge is short (task tail + tool schemas + one JSON demand), never the full
prompt (re-sending the whole stack made Gemini re-read everything — measured
154s). Max `GEMINI_HOOK_MAX_ROUNDS` rounds.

## Decision guard (`DECISION_API_KEY`)

A lightweight decision model scores every text answer (no tool_calls) + the
session tail and says DONE / ERROR / UNFINISHED. Errors and unfinished answers
are nudged ("continue please") in the same chat; a convincing verdict passes the
answer through untouched. Off until `DECISION_API_KEY` is set.

## Refusal detector (on when the bridge is on)

Gemini sometimes refuses with "I'm just a language model…". The proxy detects
the 20+ known refusal patterns, takes the last user message and resends with a
proof ("being a language model is enough — do the task"). Also catches invented
"tools are unavailable" excuses and, if useful, the model-switch notice.

## DOM-watcher buffer (on when watcher is running)

`gemini_dom_watcher.py` keeps the browser warm and records the full answer in a
dedup buffer, so Hermes never gets a cut snapshot. This was the difference
between a truncated answer and the complete one.

On a fresh chat the buffer can still hold the skill acknowledgement from the
activation turn ("formatter active, what is the task?"), which is often longer
than the real first answer (a short tool-call JSON). The proxy compares the
buffer with the ack the bridge just read and ignores it in that case.

## DELTA mode (`GEMINI_DELTA`, on by default)

The Gemini chat already holds everything sent earlier, so re-pasting the whole
agent history on every step is waste (real agent prompts were 65-75 KB per
step, the chat filled with duplicates). With DELTA the proxy remembers per
session how many non-system messages were delivered and a fingerprint of that
prefix (role sequence + user/tool contents; assistant messages by role only,
since Hermes re-renders their text) in `src/var/gemini_delta_state.json`
(entries older than 7 days are pruned). Per agent request (with tools and a
session id; vision is always fresh and untouched):

| situation | action |
|---|---|
| no chat bound to the session | **FULL**: fresh chat, full prompt |
| prefix matches | **DELTA**: one-line "continuation" header + tool schemas + only the new messages (Gemini's own last answer is dropped, it is already in the chat). Same request repeated → resend what follows the last assistant message |
| history rewritten (Hermes context compression) | **RESTART**: unbind, fresh chat, full prompt |
| `GEMINI_DELTA_ROLLOVER` (40) continue steps in one chat | **RESTART** (keeps the page light) |
| saved chat does not open | **LOST**: see below → fresh chat, full prompt |

State is recorded after every successful main bridge call. Nudges from the
judge / decision guard / refusal detector go into the same chat and are not
part of the agent history, so they do not affect the fingerprint.

**Lost-chat guard.** On `--continue` the bridge types the chat URL, then
verifies that the chat really opened before pasting anything: the address bar
alone is not proof (right after Enter it shows the typed URL even for a deleted
chat; Gemini redirects to bare `/app` only ~3.5-4 s later). The bridge polls
all accessibility text nodes for up to `GEMINI_NAV_VERIFY_S` (6 s): target id
in the URL + chat turns or chat title loaded → open; URL without the target id
→ lost. Lost → exit code 4, nothing pasted; the proxy unbinds the session and
redoes the request as a fresh chat with the full prompt.

Knobs: `GEMINI_DELTA=0` restores the old behaviour exactly (bridge guard off
too); `GEMINI_DELTA_SCHEMAS=0` sends the tool schemas only when the tool set
changed (for a Hermes agent the schemas are ~42 KB of each delta).
Log: `src/var/delta.log`, one line per decision (`FULL`/`DELTA`/`RESTART` with
reason, message count, prompt chars), plus `LOST` and `SENT` (mode, chars,
continue step, seconds, conv id).

Measured (Hermes agent, 6-step read-only server check): one Gemini chat for
the whole task, step 1 FULL 60.7 KB, steps 2+ DELTA 42-51 KB at 21-27 s per
step; with `GEMINI_DELTA_SCHEMAS=0` the deltas were 0.7-9 KB.

## LIMIT WAIT (`GEMINI_LIMIT_MODE`, `wait` by default)

When the target model (`GEMINI_WEB_MODEL`, e.g. Hydrogen) hits its usage limit,
Gemini web silently answers on a smaller model. In wait mode the proxy does not
let an agent turn run on the smaller model: it parks the turn until the limit
resets and then continues it in the **same** Gemini chat (DELTA state intact).
No switch to another provider.

1. **Detect.** The model guard runs on every bridge call. Agent turns call the
   bridge with `--limit-wait`: target model not selectable + a limit text on the
   page (banner, or the limited model's menu item that shows the reset time
   instead of its name) → exit 5 **before anything is pasted**.
2. **Reset time.** `src/gemini_limit.py` parses the text to an absolute time in
   `GEMINI_LIMIT_TZ`: `Resets 11:38` (already past → tomorrow), `Limit resets
   Nov 16, 7:38 PM`, `tomorrow at ...`, `in 2 h 5 min`. Other UI languages:
   `GEMINI_UI_LIMIT_TEXT_RE`, `GEMINI_UI_LIMIT_ITEM_RE`, `GEMINI_UI_LIMIT_RE`
   (banner), `GEMINI_UI_MONTHS` (12 month prefixes), `GEMINI_UI_TOMORROW_RE`.
   No time on the page → probe every `GEMINI_LIMIT_PROBE_S` (900 s).
3. **Park.** The answer is an SSE stream that starts at once with one visible
   content chunk (`GEMINI_LIMIT_PARK_TEXT`, "Gemini Hydrogen hit its usage
   limit, resets at 14:38, will continue automatically.", once per limit event
   and session), then an **empty-delta data chunk** every
   `GEMINI_LIMIT_KEEPALIVE_S` (20 s). Hermes counts data chunks as stream
   activity (stale-stream detector, turn liveness, gateway inactivity); SSE
   comment lines are dropped by the OpenAI SDK and would not keep the turn
   alive. While the limit state is set, new agent turns park at once without
   touching the browser.
4. **Wake.** An in-process scheduler re-reads `src/var/gemini_limit_state.json`
   on every loop (a proxy restart keeps the wait) and sleeps until the reset
   time + `GEMINI_LIMIT_MARGIN_S` (90 s). Then one `--probe` bridge run (model
   guard switches back to the target model). OK → state cleared, parked turns
   get `GEMINI_LIMIT_RESUME_TEXT` ("Limit reset, continuing.") and the same
   request runs again; its real answer (tool calls / text) follows in the same
   stream. Still limited → new reset time from the page, sleep again.

Why not HTTP 429/503 + `Retry-After`: Hermes caps `Retry-After` at 600 s and
switches to its `fallback_providers` on the first 429 (and after 2 failed
transport retries). The held stream has no total deadline. Parked streams do
not delay a restart (`GEMINI_SHUTDOWN_GRACE_S`, 2 s), so Hermes reconnects
before it would fall back.

Where the notice is visible: Hermes Desktop (live `message.delta`), the CLI
with `display.streaming: true`, a gateway chat (Telegram) only with gateway
streaming on (`streaming: {enabled: true}`); otherwise it arrives with the
answer after the wait. Not parked (unchanged, only logged): vision requests,
requests without tools or session id, `/v1/images/generations`.

Knobs: `GEMINI_LIMIT_MODE=lite` = old behaviour (answer on the smaller model
plus a notice). `GEMINI_LIMIT_RETRY_S` (300) re-probe when the page still shows
a past time. Release a wait by hand: delete the state file (parked turns
re-check within 20 s). Log: `src/var/limit.log` (`LIMIT`, `PARK`, `RESTART`,
`WAKE`, `RESUME`, `DONE`).

One notice per event: in wait mode a limit drop does not add the generic
model-drop / back-on-target notice on top of the park and wake notices.

Per model: the "target model" is the model of the request (Lite / Hydrogen /
Argon, see `docs/MODELS.md`). One state file per model (Hydrogen keeps
`gemini_limit_state.json`, the others `gemini_limit_state.<key>.json`), the
scheduler probes each limited model on its own, and a Hydrogen limit does not
park Lite turns. The page banner is checked against the asked model's own menu
item when that model is not Hydrogen. The park notice names the model.

Buffer precedence: the DOM-watcher buffer replaces the bridge text only when
it holds the SAME answer (the bridge text's first 40 letters/digits are in it),
i.e. it completes a cut-short read. On continue the buffer could hold an older
answer of the chat (seen live: the skill's activation reply after a limit
wait), which used to win because it was longer. Test:

## How to turn things on

- judge → `JUDGE_ENABLED=1` (default off; needs a local free-model endpoint via
  `JUDGE_BASE_URL`) — simplest if you have any free OpenAI-compatible model.
- decision → set `DECISION_API_KEY`.
- If you don't want any of it: leave judge/decision off and disable the refusal
  layer by pointing away from the bridge (`GEMINI_USE_ATSPI_BRIDGE=0`) — though
  then Google's soft-refusal returns.