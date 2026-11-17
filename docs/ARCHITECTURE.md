# Architecture

## Why a browser, and not the Gemini API

Google's Gemini API has limits and is not free. `gemini.google.com` website is
free in a browser. Google soft-refuses most *non-browser* clients
(`gemini_webapi`/curl_cffi are detected by TLS-fingerprint and the missing
BotGuard context even with a valid cookie jar). The only transport that reliably
passes is **a real, logged-in Firefox** driven by pure X11 input — no CDP,
Marionette, WebDriver, which would also be detectable.

## Layering

```
your client (curl / OpenAI SDK / Hermes)
        │  POST /v1/chat/completions  (OpenAI format)
        ▼
src/server.py            FastAPI, port 9658
   │  - flatten messages, inject tool schemas
   │  - keep session_id → gemini URL  (gemini_sessions.json)
   │  - rate limit (protect the Google account)
   │  - spawns the bridge (one request at a time)
   ▼
src/gemini_atspi_bridge.py      /usr/bin/python3 (has pyatspi)
   │  - ensure Xvfb :2 + fluxbox + Firefox(gemini-pump) running
   │  - xdotool: ctrl+shift+o (new chat) / type prompt / Enter
   │  - read answer from the AT-SPI accessibility tree
   ▼
Firefox  ── logged into Google ──►  gemini.google.com
```

Two optional pieces sit beside the route:

- `src/gemini_dom_watcher.py` — a daemon that keeps the browser alive and
  continuously scrapes the AT-SPI tree into a **dedup buffer**
  (`dom_buffer.json`), so a long answer is captured in full instead of a cut
  snapshot. When it's alive, the one-shot bridge passes `--keep-open` to reuse
  the same warm browser (no cold start per request).
- `src/gemini_markdown.py` — rebuilds the answer as Markdown from the page's
  real HTML structure (headings, lists, code fences, tables, bold) instead of
  flattened accessible text. Used inside the bridge and the watcher.

## Session persistence (one chat per caller session)

Gemini Web has no API sessions. The proxy keeps a small DB
(`gemini_sessions.json`): `{session_id: "https://gemini.google.com/app/<id>"}`.

- First request for a `session_id` → the bridge opens a **fresh chat**
  (Ctrl+Shift+O) and records the new `<conv_id>` from the address bar.
- Later requests with the same `session_id` → the bridge opens that saved URL
  (`--continue <conv_id>`) so the conversation resumes.

That's what lets Hermes keep one Gemini chat per Hermes session and survive
compression/agent restarts (`cache_scope_id` is the stable lineage root).

## Tool calls (emulation)

Gemini Web has no function-calling protocol. The proxy emulates it:

1. tool parameter **schemas are injected into the prompt as text**;
2. the model replies with a JSON object like
   `{"tool_call":{"name":"...","arguments":{...}}}`;
3. the server parses it back into OpenAI `message.tool_calls`;
4. `role:tool` results are flattened back into the conversation text on the
   next turn.

So any OpenAI tool-using client works, but there is deliberately no competing
format text in the injection — the account's own `tool-call-formatter` skill is
what tells the model the exact envelope ("skill tool-call-formatter" is sent as
the first activation turn).

## Fail-safes

- **Retry with fresh session** on stream loss / empty response (bounded).
- **Overall request deadline** so a stalled upstream can't hang the caller.
- **Disconnect-aware**: if the HTTP client is gone, don't burn Google quota.
- **SSE is chunked** so streaming clients see progress (never one giant blob).
- **Rate limiter + cooldown on 429** to keep the Google account un-banned.
- **Judge hook / decision guard / refusal detector**: optional resilience
  layers that catch "the model said it will, then didn't" and nudge once or
  twice. All off unless configured (see `docs/RESILIENCE.md`).

## The one-window rule

Only one browser session exists at a time. The bridge holds an exclusive lock,
so concurrent callers queue rather than tear down each other's Xvfb/Firefox.
The DOM-watcher and the image job share the same lock/profile/display.