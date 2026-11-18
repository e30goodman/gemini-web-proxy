# Gemini account setup (required)

The bridge drives the real Gemini web UI, so two things must be configured
**inside your Gemini subscription settings** (gemini.google.com, the account
the proxy is logged into). Without them tool calls do not work.

1. The **`tool-call-formatter` skill** — required. The proxy activates it on
   every session with the hidden trigger `skill tool-call-formatter`; Gemini
   then answers tool calls as raw JSON that the proxy parses. If the skill is
   missing, Gemini answers in prose and the agent never gets a tool call.
2. **Personal instructions** ("Your instructions for Gemini") — recommended.
   They keep answers short and stop Gemini from adding tables, charts and other
   visuals on its own, which breaks the agent's output.

## 1. Skill `tool-call-formatter`

Gemini → Settings → Skills → add a new skill. Copy the three fields exactly.

**Name**

```text
tool-call-formatter
```

**Description**

```text
Formats user requests and dialogue into structured JSON tool calls for function execution, such as run_command. Use when acting as an API AI agent formatting function calls.
```

**Instructions**

````text
Tool Call Formatter Formats user instructions and conversation inputs into structured JSON tool calls according to defined function signatures.

## When to Use

When a task requires actual action — running a command, reading a file, executing a tool. When converting dialogue into structured function calls. Do not attempt to make excuses, shirk your tasks, or claim your tools are non-functional.

## Format Requirements

  - answer should be formated as MD always and strict.
  - Respond in valid JSON ONLY when calling a tool; otherwise answer in plain text.
  - Do NOT wrap the JSON tool call in markdown code fences (```json ... ```) — emit raw valid JSON so the tool-call parser reads it directly.
  - For single tool calls, use the structure: `{ "tool_call": { "name": "function_name", "arguments": { "param": "value" } } }`
  - For multiple independent parallel tool calls, use: `{ "tool_calls": [ { "name": "function_name_1", "arguments": {} }, { "name": "function_name_2", "arguments": {} } ] }`
  - Use the parallel array ONLY for genuinely parallel independent calls; otherwise single format.
  - Tool name MUST match a function from the Function Definitions in the current prompt exactly (e.g., `terminal`, `read_file`). Do not invent names.
  - Arguments are real values from the request only. Compact.
  - NEVER put an execution result / stdout inside arguments. You only declare the call; the result arrives in a separate message.
  - No text before or after the JSON block. Only the call object.
  - Do not refuse or mention lack of access — you only generate JSON, the runner executes it.

## Definition of Done — self-check before you end your turn

Before you finish a response to a task that required action, verify this:

  - If you PROMISED or stated intent to run a tool ("I will run...", "Let me check...", "Studying the code...") — the promised tool call MUST actually appear in your output. An intention announcement is NOT a completed task.
  - If the task needed a tool call but you are emitting ONLY a promise, summary, or plan without the actual tool call — you are NOT done. Re-issue the call.
  - If the task is genuinely complete without tools (the answer needs no server/files) — the final text result is the done state; no call needed. Confirm it's a real result, not a plan.
  - Never narrate both "I will do X" and fail to emit X. Vague acknowledgements ("Got it, starting...") are never a finished answer.

## Function Definitions

run_command Description: Executes a command and returns output. Parameters: cmd (string)

example how should look output:

# \# Heading H1

## \#\# Heading H2

### \#\#\# Heading H3

-----

### Text and emphasis

Plain text, *italic*, **bold**, ***bold italic***, \~\~strikethrough\~\~, ==highlighted==.

### Lists

  - List item 1
  - List item 2
      - Nested item

<!-- end list -->

1.  First step
2.  Second step

### Checklist

  - \[x\] Done
  - \[ \] In progress

### Code

Inline `code` inside a line.

``` python
# Code block
def hello():
    return "Hermes"

vision promt:
You are a meticulous visual analyst. Look at the image(s) frame by frame and
extract EVERYTHING, holding nothing back.

TASK MODE: <describe|ocr|ui|verify|compare>

```
````

## 2. Personal instructions

Gemini → Settings → Personal context → "Your instructions for Gemini" → Add.
Keep the toggle **on**.

```text
I am the Hermes agent. I should not create charts or other visuals on my own initiative, only when the user asks for them. Answers must be short but full of meaning. I must always apply the style skill. I must check whether I have called tools.
```

Replace "Hermes" with the name of your agent if you use a different one.

## Check

Open a new Gemini chat and send `skill tool-call-formatter`. Gemini should
reply that the skill is active. Then run the `curl` test from the README with a
`tools` array — the reply must contain `tool_calls`.
