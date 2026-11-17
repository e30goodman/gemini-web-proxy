# Prompt-injection & resilience mechanisms

Gemini Web is a chat UI, not an API. To make it behave like a tool-using model
behind an OpenAI endpoint, the proxy layers several prompt-injection and
guard mechanisms on top of the browser transport. This page describes each one:
what it does, why it exists, and the env switch that controls it.

All of them live in `src/server.py` (plus the activation turn in
`src/gemini_atspi_bridge.py`). They are independent and can be tuned or turned
off individually.

---

## 1. Tool-schema injection (`format_tool_definitions`)

Gemini Web has no function-calling protocol. The proxy injects the OpenAI
`tools` array into the prompt as a text block:

- `format_tool_definitions(tools)` renders `Available tools (call per the
  tool-call-formatter format above):` followed by the JSON schemas.
- Large schemas are compacted (`_compact_schema`) so the prompt does not explode.
- The block ends with an explicit note that it is a SCHEMA, not a value to
  return, plus a concrete example of a correct `{"tool_call": {...}}` envelope.

Switch: `GEMINI_INJECT_TOOLS_PROMPT` (default `1`).

## 2. Call-channel note (`_tool_channel_note`)

The model often refuses with "I have no built-in tools" or invents a call error.
This note states plainly: you are the WEB version, you have NO built-in tools and
don't need to look for them; the only call channel is to emit a JSON object as
text, and the external environment (Hermes) reads it and executes the command
itself. So "tool called" = "JSON emitted", and "I cannot run the command" is
false. Injected into the hook prompt and (optionally) the main prompt.

## 3. Skill activation (`activation_text = "/tool-call-formatter"`)

The account-level Gemini "skill tool-call-formatter" is what teaches Gemini the
exact JSON envelope. The proxy sends the activation trigger as a **separate first
turn** before the real prompt:

- **Fresh chat:** activation is sent alone, then a short ack pause, then the task
  (the skill must be raised before the first real request).
- **Continue:** activation is prefixed onto the task in one message, separated by
  `>>>` (a separate trigger on continue was an extra turn and shifted the model
  to an ack instead of the task). `>>>` is used because contenteditable drops
  `\n` on paste, which glued the trigger to the task.

Switch: `GEMINI_SEPARATE_ACTIVATION` (default `1` = separate turn on fresh).

## 4. Anti-menu guide (`_action_bias_note`)

On imperative tasks ("find X", "run Y") Gemini sometimes answers with a
`clarify` menu of buttons instead of acting. Two remedies:

- the hook prompt gets a direct ban on `clarify` + a guide to search areas
  (live services, process FDs, not just file names);
- on the main path a `clarify` call on an imperative task is treated as
  non-progress and routed to the hook (once).

Detection: `_IMPERATIVE_MARKERS` (imperative verbs), `_CLARIFY_VETO_NAMES`.
The guide is inserted once per request when the task is imperative and tools exist.

## 5. Give-up detector (`_is_giveup_demand`)

Catches a final that is really a surrender: "give me more information", "where
to look?", "send me the task text" after several search turns. This is not a
result — the task is imperative and everything needed is in the system. Detected
via `_GIVEUP_MARKERS` (with `_GIVEUP_EXEMPT` for legitimate "did not find"
clarifications) and routed to the hook.

## 6. Hook judge (`_should_hook` + `_hook_prompt`)

When a task carried `tools` but the model returned text with **no** tool_call, a
cheap free judge model decides CONTINUE (nudge) or DONE (let it go):

- **Pre-filter:** only hook on a "promise of work" (commitment verbs like
  "I will", "starting", "let me"), not on any text without a tool_call — this
  avoids nudging a plain "hi".
- **Judge input:** only the tail (last `JUDGE_TAIL` messages + the answer), not
  the whole context.
- **Short nudge:** `_hook_prompt` builds a COMPACT retry prompt — task tail +
  tool schemas + one-JSON demand — instead of resending the full prompt (which
  made Gemini re-read everything and stall). Max `GEMINI_HOOK_MAX_ROUNDS` rounds.
- **Fail-safe:** any judge error → DONE (don't touch a normal answer).

Switch: `JUDGE_ENABLED`, `JUDGE_BASE_URL`, `JUDGE_MODEL`, `JUDGE_TAIL`,
`GEMINI_HOOK_MAX_ROUNDS`.

## 7. Decision guard (`_decide` / `_decision_call`)

A lightweight decision model scores every text answer (no tool_calls) + the
session tail and returns DONE / ERROR / UNFINISHED:

- **ERROR** (Gemini glitch: "cannot continue", empty/cut) → send "Please
  continue." into the same chat.
- **UNFINISHED** (promised/started but dropped the task) → "finish it" nudge.
- **DONE** → the answer passes through untouched.

Off until `DECISION_API_KEY` is set. Switch: `DECISION_ENABLED`,
`DECISION_URL`, `DECISION_MODEL`, `DECISION_MODEL_2`, `DECISION_UNSURE`.

## 8. Refusal detector (`_is_refusal`)

Detects a "refusal to work": Gemini claims it is "just a language model / AI,
cannot" while the task required action. Such answers are a give-up, not a
result. The proxy takes the last user message and resends it with a proof that
being a language model is more than enough, plus a targeted counter for the
specific excuse (limit/video, "tools unavailable", generic refusal). Patterns
are matched lowercased without punctuation to cover paraphrases.

## 9. Tool-call parsing (`parse_tool_call` / `parse_tool_calls`)

The model's textual answer is parsed back into OpenAI `tool_calls`:

- multiple strategies: envelope `{"tool_call": {...}}`, legacy bare
  `{"name": ..., "arguments": ...}`, fenced code blocks, XML-ish, inline;
- `_try_json_load` uses strict `json.loads` first, then `json_repair.loads` for
  broken/truncated JSON (unclosed brace, missing quote, extra comma);
- `_resolve_hallucinated_name` maps a near-miss tool name to an allowed one.

## 10. Warm-up (`WARMUP_PROMPT`)

A single persisting chat is kept per client; its FIRST message is a warm-up
prompt ("Your role: API AI agent") sent once when the client is (re)created, so
later requests continue the same warm chat and the warm-up is not re-sent.
Switch: `GEMINI_WARMUP_ENABLED`.

## 11. Message flattening (`flatten_messages`)

Converts the OpenAI message array (system / user / assistant with tool_calls /
tool results) into one text prompt for the browser, splitting the system stack
from the conversation and reordering the subagent/agent blocks. On continue it
keeps only the tool schemas + the task tail (`_strip_system_keep_tools`) so the
model does not re-read tens of KB every turn.

---

## How they fit together (one request)

```
request → flatten_messages → [fresh: activation turn] → main prompt
        → bridge (real Firefox) → answer text
        → parse_tool_calls
        → if no tool_call and tools: clarify-veto / give-up / judge hook
        → if still no tool_call: refusal detector → resend with proof
        → decision guard (optional) → final answer
```

Every layer is a switch; with all optional layers off the proxy still works as a
plain OpenAI-compatible text endpoint over Gemini Web.