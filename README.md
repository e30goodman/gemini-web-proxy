# Gemini Web Proxy

OpenAI-compatible proxy over **Gemini Web** (gemini.google.com browser session) —
no Google API key, free. It drives a real, logged-in Firefox over X11 + AT-SPI
(xdotool / pyatspi) and exposes it as `POST /v1/chat/completions`.

**In short: it lets you use your Gemini subscription as if it were an API.** A
strong coding model, available to any OpenAI-compatible client in your
environment — Hermes, OpenCode, a DeepSeek harness, or anything else that speaks
`/v1/chat/completions`. You log into Gemini once in a browser; the proxy turns
that session into a normal API endpoint with tool calling, streaming, vision and
image generation.

Google soft-refuses `gemini_webapi`/curl_cffi (TLS-fingerprint + no real
BotGuard JS context), so the only working transport is a real browser. This
project is that transport, packaged as a service.

## What you get

- **`src/server.py`** — FastAPI OpenAI-compatible endpoint on `127.0.0.1:9658`:
  `/v1/chat/completions`, `/v1/models`, `/v1/images/generations`, `/healthz`.
  Text, **tool-call emulation** (Gemini Web has no native function API — schemas
  are injected as text, the JSON answer is parsed back into `tool_calls`),
  session persistence (one Gemini chat per Hermes session), **DELTA mode**
  (on continue only the new messages are sent, not the whole history; lost or
  rewritten chats fall back to a fresh chat — see `docs/RESILIENCE.md`),
  **LIMIT WAIT** (target model out of quota → the agent turn is parked with a
  visible notice and continues in the same chat when the limit resets, time
  read from the page — no fallback to the smaller model or another provider),
  **EFFORT GUARD** (every bridge session re-checks the model's reasoning level,
  which Gemini web keeps resetting to Low, and sets `GEMINI_WEB_EFFORT`,
  default Medium — see `docs/EFFORT.md`), **MODELS** (per-request model
  `gemini-auto` / `gemini-hydrogen` / `gemini-neon` and thinking level from the
  client's `reasoning_effort`, switched on the page before sending, one limit
  state per model — see `docs/MODELS.md`), streaming SSE, mechanical rate limiting, judge/decision hooks.
- **`src/gemini_atspi_bridge.py`** — drives the real Firefox on a headless
  virtual display (`Xvfb :2`): types the prompt via xdotool, reads the answer
  from the AT-SPI accessibility tree. Zero CDP/marionette/geckodriver, so
  Google's automation detection has nothing to see.
- **`src/gemini_dom_watcher.py`** — optional daemon that keeps the browser warm
  and accumulates the answer in a dedup buffer (a full answer, not a cut
  snapshot) — the major speed fix over a cold-start per request.
- **`src/gemini_markdown.py`** — rebuilds Gemini's answer as Markdown from the
  page's HTML structure (headings, lists, code fences, tables) instead of flat
  paragraph text.
- **`src/gemini_model.py`** — model guard: keeps the session on your chosen
  model, switches it back when Google resets it.
- **`src/gemini_image_job.py`** + `scripts/gemini_grab_answer.py` +
  `scripts/gemini_viewer_download.py` — image generation through the real
  browser (the cookie API path silently aborts on Google; the browser GUI path
  works).
- **`scripts/gemini-login.sh`** — **log into the hidden browser** over VNC so
  the proxy has working cookies (see `docs/LOGIN.md`).
- **Hermes integration** — `hermes-integration/provider_plugin.py` (one Gemini
  chat per Hermes session) + `docs/HERMES_INTEGRATION.md`.

## Architecture (30-second version)

```
client → :9658 server.py ── session_id ──► gemini_atspi_bridge.py
                                            │  xdotool type + AT-SPI read
                                            ▼
                              Firefox (gemini-pump) on Xvfb :2
                                            │ logged into Google
                                            ▼
                                      gemini.google.com
```

- The proxy reads cookies from the logged-in Firefox profile
  (`FIREFOX_PROFILE`) and passes them to the bridge.
- Hermes sends its `session_id`; server keeps `gemini_sessions.json`
  (`session_id → gemini URL`) so each Hermes session resumes its own chat.
- `gemini_dom_watcher` (when running) owns the browser; the one-shot bridge's
  teardown is skipped (`--keep-open`) so the warm browser is reused.

## Requirements

- Linux with `Xvfb`, `fluxbox`, `xdotool`, `xclip`, a window-capable Firefox
  (ESR works), and `pyatspi` (**system** Python only — the bridge refuses any
  other interpreter because that's where pyatspi is installed).
- Python 3.11+ for the server (`requirements.txt`: fastapi, uvicorn,
  gemini-web-api, curl_cffi, json-repair, dotenv).
- A Firefox profile logged into the Google account that should serve Gemini
  (`scripts/gemini-login.sh` makes this easy).

## Quick start

```bash
# 1. install system deps (Alpine example)
apk add xvfb fluxbox xdotool xclip py3-atspi x11vnc firefox-esr

# 2. Python deps
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 3. log into the browser the proxy will use
cp .env.example .env
./scripts/gemini-login.sh          # see docs/LOGIN.md

# 3b. in that Gemini account add the tool-call-formatter skill and the
#     personal instructions -- REQUIRED for tool calls, see
#     docs/GEMINI_ACCOUNT_SETUP.md

# 4. run
.venv/bin/python src/server.py       # http://127.0.0.1:9658
# + in a second shell, the warm-browser daemon:
python3 src/gemini_dom_watcher.py
```

Then test:

```bash
curl -s http://127.0.0.1:9658/v1/models
curl -s http://127.0.0.1:9658/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-web-flash","messages":[{"role":"user","content":"2+2?"}]}'
```

systemd units (install path `/opt/gemini-web-proxy`) are in `systemd/`.

## Configuration

All knobs are env vars, see `.env.example`. The important ones:

| var | default | meaning |
|---|---|---|
| `PORT` / `HOST` | `9658` / `127.0.0.1` | listener |
| `FIREFOX_PROFILE` | `~/.mozilla/firefox/gemini.default` | profile whose cookies the proxy uses |
| `GEMINI_PUMP_PROFILE` / `GEMINI_DISPLAY` | `~/.mozilla/firefox/gemini-pump.default` / `:2` | profile+display the bridge drives |
| `GEMINI_USE_ATSPI_BRIDGE` | `1` | route through the real browser (keep on) |
| `PROXY_API_KEY` | empty | require this bearer token from clients |
| `GEMINI_RATE_MAX_PER_MIN` / `_HOUR` | `4` / `40` | protect the Google account from bans |
| `JUDGE_ENABLED`, `DECISION_API_KEY` | off | optional resilience layers |
| `GEMINI_DELTA` / `GEMINI_DELTA_ROLLOVER` | `1` / `40` | send only new messages on continue; fresh chat after N steps |
| `GEMINI_LIMIT_MODE` / `GEMINI_LIMIT_TZ` | `wait` / system time zone | park agent turns while the target model is limited (`lite` = answer on the smaller model) |
| `GEMINI_SEND_CONFIRM` / `GEMINI_SEND_CONFIRM_S` | `1` / `14` | after Enter, check on the page that Gemini really took the message; if not, a continued chat moves to a fresh one instead of reading the old answer back |
| `GEMINI_DELTA_SCHEMAS` / `GEMINI_DELTA_SCHEMAS_EVERY` | `1` / `8` | `1` = every delta step resends the tool schemas (otherwise Gemini can "forget" its tools mid-task); `0` = only when the tool set changed, plus every N steps as a reminder |
| `GEMINI_REFUSAL_ROUNDS` | `2` | how many times a refusal / fake "no tools" excuse / Gemini internal error is retried (internal errors and "injection" refusals go to a fresh chat) |
| `GEMINI_SKILL_MODE` | `chip` | how the tool-call skill is attached: `chip` (@ menu), `text` (old `/skill` trigger) or `none`; a `skill_mode` file next to the bridge overrides it without a restart |
| `GEMINI_CHIP_ACTIVATION_TURN` | `1` | on a fresh chat the skill chip first gets its own short turn, then the task |
| `GEMINI_DELTA_MAX_CHAT_CHARS` | `500000` | fresh Gemini chat once this much text went into one chat |
| `GEMINI_SESSION_NOTE` | `1` | add the client session id to every prompt, so after a context loss the model can reload the history with `session_search` |
| `GEMINI_MODEL_CHECK_TTL` | `120` | skip the model-picker check on a continued chat verified less than N s ago |
| `WATCH_IDLE_TIMEOUT_S` / `WATCH_IDLE_NIGHT_S` | `900` / `900` | close the idle browser after N s (daytime / 00:00-07:00, `WATCH_NIGHT_FROM` / `WATCH_NIGHT_TO`) |
| `GEMINI_BROKEN_CALL_RETRIES` | `2` | when Gemini writes a tool call with invalid JSON, ask it this many times to re-emit valid JSON; then a short notice instead of the raw text |
| `GEMINI_WEB_EFFORT` | `medium` | reasoning level the bridge enforces every session (`low` / `medium` / `high` / `off`); a request's `reasoning_effort` overrides it |
| `GEMINI_WEB_MODEL` | `hydrogen` | model for ids other than `gemini-auto` / `gemini-hydrogen` / `gemini-neon` (the model is chosen per request) |

## Docs

- `docs/FIREFOX_PROFILE.md` — optional `user.js` lines that cut Firefox memory.
- `docs/GEMINI_ACCOUNT_SETUP.md` — **required**: the `tool-call-formatter`
  skill and personal instructions to add in your Gemini subscription settings.
- `docs/ARCHITECTURE.md` — the transport, tool-call emulation, session mapping.
- `docs/LOGIN.md` — how to log into the hidden browser (VNC) step by step.
- `docs/HERMES_INTEGRATION.md` — wiring into Hermes, one chat per session,
  vision, image gen.
- `docs/PROMPT_INJECTION.md` — every prompt-injection / guard mechanism
  (schema injection, call channel, skill activation, anti-menu guide, hook
  judge, decision guard, refusal detector, tool parsing, warm-up).
- `docs/RESILIENCE.md` — guard dogs (judge, decision, refusal, rate limiter),
  DELTA mode, LIMIT WAIT.
- `docs/EFFORT.md` — the reasoning-level (effort) guard.
- `docs/MODELS.md` — per-request model and thinking level.