# Integrating with Hermes

This proxy was built to be a **model provider for Hermes** (Hermes Agent).
Integration has four parts, each independent:

## 1. Provider plugin (one chat per Hermes session)

`hermes-integration/provider_plugin.py` overrides the provider's
`build_extra_body` so the raw Hermes `session_id` (and `cache_scope_id`, the
stable lineage root) reach the proxy. The proxy keeps one Gemini chat per Hermes
session and resumes it (`--continue`) on later turns — without this the session
id never leaves Hermes and every turn would start a fresh chat.

Install it at
`$HERMES_HOME/plugins/model-providers/gemini-web-proxy/__init__.py` **and in
every profile that uses the proxy**
(`$HERMES_HOME/profiles/<profile>/plugins/model-providers/gemini-web-proxy/__init__.py`).
A profile without its own copy sends no session id: every agent step opens a
new Gemini chat with the full prompt and DELTA mode never kicks in. Then add a
custom provider to the Hermes config:

```yaml
custom_providers:
  - name: gemini-web-proxy
    base_url: http://127.0.0.1:9658/v1
    api_key: <PROXY_API_KEY if you set one>
    model: gemini-hydrogen
    discover_models: false
    models:
      gemini-auto: {context_length: 1000000}
      gemini-hydrogen: {context_length: 1000000}
      gemini-neon: {context_length: 1000000}
```

and the same 3 models under the named provider block:

```yaml
providers:
  gemini-web-proxy:
    name: Gemini Web Proxy
    base_url: http://127.0.0.1:9658/v1
    discover_models: false
    models:
      gemini-auto: {context_length: 1000000}
      gemini-hydrogen: {context_length: 1000000}
      gemini-neon: {context_length: 1000000}
```

Hermes has no display-name field for a model: Desktop builds the label from
the id, so the picker shows "Gemini Auto" / "Gemini Hydrogen" / "Gemini Neon".
`discover_models: false` keeps the picker at exactly these 3 (`/v1/models`
lists the same 3). Every other id still works and means Hydrogen (see
`docs/MODELS.md`). The model is switched on the Gemini page per request, in the
same chat. The thinking level comes from Hermes' reasoning setting
(`/reasoning <level>`, the Desktop effort dial, `agent.reasoning_effort`): the
plugin sends it as top-level `reasoning_effort` (low / medium / high; unset ->
omitted, the proxy uses `GEMINI_WEB_EFFORT`). Do this in every profile that
uses the proxy and refresh the model picker.

## 2. Vision (text description of images)

Point a script at `POST /v1/chat/completions` with the images as `data:` URIs
(max 4, ~10MB each in the reference setup). A full example CLI is
`vision_view.py` (modes: describe / ocr / ui / verify / compare). Models used
in the reference setup: `gemini-web-lite → gemini-web-flash → gemini-web-pro`.
Vision answers are grounded with `証` (checked) / `仮` (model guess) markers —
the reference CLI returns them and callers relay them unchanged.

## 3. Image generation

`POST /v1/images/generations` accepts OpenAI names (`gpt-image-2`, `gpt`, …)
mapped to the Gemini model and returns `data[0].b64_json`, so `gpt-image` and
nbcraft clients work as-is. Because a cookie-driven image POST sometimes
silently aborts on Google, the reference pipeline also offers a **browser-GUI
path**: `src/gemini_image_job.py` pastes the prompt into the real Firefox chat,
clicks Gemini's own download button, and saves the file. Common rule: verify the
generated image with vision before shipping it.

## 4. Model guard + tool-call formatter skill

- `src/gemini_model.py` keeps the session on the model of the request
  (`gemini-auto` / `gemini-hydrogen` / `gemini-neon`, default Hydrogen) and
  switches it back when Google resets it, and reports the drop.
- The account-level Gemini "skill tool-call-formatter" is what actually teaches
  Gemini the JSON tool-call envelope; the proxy just supplies the schemas. First
  North of an agent turn, the bridge sends the activation trigger
  (`skill tool-call-formatter`) as a separate first turn, then the real prompt.

## Prompt-flow notes relevant to Hermes

- On a **fresh** session the full native Hermes system stack (SOUL + memory +
  skills + `format_tool_definitions`) is sent once, so the model has everything.
- On **continue**, only the tool schemas + the task tail are re-sent (sending
  the whole stack every turn makes Gemini re-read tens of KB and stall — this
  was measured: 154s → 24s).
- Debug: last prompt → `/tmp/gemini_debug_last_prompt.txt`, last answer →
  `/tmp/gemini_debug_last_response.txt`, plus a 12-file ring buffer under
  `/tmp/gemini_prompt_ring/`. Use these to prove exactly what reached Gemini.