# MODELS - per-request model and thinking level

The client (Hermes) picks the model (model id) and the thinking level (its
reasoning setting) for every request. The bridge switches the Gemini page to
that model and level in the same chat right before sending, so a Hermes session
keeps its Gemini chat (and its DELTA state) when the user switches the model in
the middle of it.

## Model ids

| Model id          | Label in Hermes  | Gemini menu item matched by | key        |
|-------------------|------------------|-----------------------------|------------|
| `gemini-lite`     | Gemini Lite      | `Flash-Lite` (3.5)          | `lite`     |
| `gemini-hydrogen` | Gemini Hydrogen  | `Hydrogen` (4, default)     | `hydrogen` |
| `gemini-argon`    | Gemini Argon     | `Argon` (4)                 | `argon`    |

- `/v1/models` lists only these 3 (the Hermes picker shows exactly 3). Hermes
  has no display-name field for a model: Desktop builds the label from the id
  (`gemini-lite` -> "Gemini Lite"), so the ids are the names.
- `gemini-web-lite` / `gemini-web-hydrogen` / `gemini-web-argon` are accepted
  as aliases, not listed.
- Every other id still works and means Hydrogen: `gemini-web-flash`,
  `gemini-web-default`, `gemini-web-pro`, `gemini-web-flash-advanced`,
  `gemini-web-pro-advanced`, `gemini-web-proxy/<id>`, no model. (Env
  `GEMINI_WEB_MODEL` changes that fallback, default `hydrogen`.)

## How it works

- `src/server.py`: `WEB_MODELS` (id -> key) and `MODEL_ALIASES`,
  `_set_req_model(body)` puts the key and the level into context vars;
  `_bridge_env()` hands them to the bridge as `GEMINI_WEB_MODEL=<key>` /
  `GEMINI_WEB_EFFORT=<level>`.
- `src/gemini_model.py`: `MODELS` (key -> menu name regex, subtitle regex,
  strict). `ensure_model()` finds the picker and the menu item by accessible
  name (no coordinates), switches, re-reads the picker label to verify. On
  failure `LAST_FAIL` says why: `limit` / `locked` / `missing` / `verify` /
  `picker`.
- A limited item loses its model name and shows only its subtitle plus the
  limit text (`<subtitle> Limit resets ...`),
  so the limit is matched by the item subtitle - only the asked model's item
  counts.

## UI language

The model names (`Hydrogen`, `Flash-Lite`, `Argon`) are the same in every UI
language. Subtitles and upsell words default to English; for another UI
language override them (regexes):

| Var | Default | What |
|---|---|---|
| `GEMINI_UI_HYDROGEN_DESC_RE` | `Everyday` | Hydrogen item subtitle |
| `GEMINI_UI_LITE_DESC_RE` | `Fast answer\|Quick answer` | Flash-Lite item subtitle |
| `GEMINI_UI_ARGON_DESC_RE` | `Code (and\|&) analysis\|Coding` | Argon item subtitle |
| `GEMINI_UI_LOCKED_RE` | `(Upgrade\|Subscribe\|Get Google AI\|Google AI (Pro\|Ultra)\|Unlock)` | upsell words of a locked item |
| `GEMINI_UI_LIMIT_ITEM_RE` | see `docs/RESILIENCE.md` | limit text of a menu item |
| `GEMINI_MODEL_UNAVAILABLE_TEXT` | `{model} is not available on this account` | answer when a strict model cannot be used |

Override the regexes for a non-English UI.

## Strict models (Lite, Argon)

Hydrogen keeps the fail-open behaviour (answer on whatever the page has, with
the model-drop notice). Lite and Argon are strict: if the item is missing,
locked (no subscription: upsell words or disabled state) or does not take, the
bridge sends nothing and exits 7, and the client gets a normal answer with the
plain reason, e.g. `Argon is not available on this account`. Argon was not
tested live (no subscription on the test account), only offline
.

## Limits (see docs/RESILIENCE.md, LIMIT WAIT)

One limit state per model: Hydrogen keeps `src/var/gemini_limit_state.json`,
the others get `gemini_limit_state.<key>.json`. A Hydrogen limit does not park
Lite turns and the other way round. A limited Lite or Argon in an agent turn is
parked and woken the same way as Hydrogen (the wake-up probe checks that model).
The park notice names the model: `Gemini Flash-Lite hit its usage limit, ...`.

## Thinking level (see docs/EFFORT.md)

Read from the request: top-level `reasoning_effort` (what Hermes sends to an
OpenAI-compatible provider, see `hermes-integration/provider_plugin.py`), else
`reasoning.effort`, else `extra_body`. Gemini has low / medium / high; others
map to the nearest: `none`, `minimal` -> low, `xhigh`, `max`, `ultra` -> high,
`reasoning.enabled: false` -> low. Not sent -> `GEMINI_WEB_EFFORT` (default
medium). All 3 models have the control (Lite too); a model without it ->
logged, the request continues.

Verified live: `gemini-lite` without a level -> page on Flash-Lite, level set
to medium; `gemini-hydrogen` with `reasoning_effort: high` -> page on Hydrogen,
level high; an old id (`gemini-web-flash`) -> Hydrogen, medium.

## Add a model

1. `gemini_model.MODELS`: new key with the menu name regex (`item`), the item
   subtitle regex (`desc`, for the limited state) and `strict`.
2. `server.WEB_MODELS`: model id -> that key (it shows up in `/v1/models`).
3. `gemini_limit.NAMES`: key -> name for the park notice.
4. Hermes: add the id to the provider's `models` (see
   `docs/HERMES_INTEGRATION.md`) in every profile, refresh the model picker.
5. Send one request with the new model id and check the log.
