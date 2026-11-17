# EFFORT GUARD

Gemini web resets the reasoning level of the model to **Low** now and then
(seen after page loads: fresh browser, reload, opening a saved chat by URL),
and answers on low are noticeably worse. On EVERY bridge session (fresh chat
and continue, also `gemini_image_job.py`) the bridge reads the level right
after the model guard, switches it to the target (default Medium) and
verifies it. Code: `src/gemini_model.py` (`ensure_effort`).

## Where the control is (a11y tree, no coordinates)
- Mode picker button (`GEMINI_UI_PICKER_RE`) -> submenu `Reasoning level`
  (`GEMINI_UI_EFFORT_MENU_RE`) -> items `Low` / `Medium` / `High`, the current
  one is checked.
- When the level is not low, the picker label names it (`... using Hydrogen
  Medium`). That is the fast path: one AT-SPI Collection lookup, ~0.05-0.1 s,
  no menu. On low the label is just the model name -> the menu is opened,
  `Medium` clicked, the label re-read (~1.1-1.3 s measured).
- No control (another model, UI change): logged, the request goes on.

## Env (read by the bridge per request, no proxy restart)
| Var | Default | What |
|---|---|---|
| `GEMINI_WEB_EFFORT` | `medium` | `low` / `medium` / `high` / `off` (no guard) |
| `GEMINI_UI_EFFORT_MENU_RE` | `^(Reasoning level\|Thinking level\|...)` | submenu name |
| `GEMINI_UI_EFFORT_LOW_RE` / `_MEDIUM_RE` / `_HIGH_RE` | `Low` / `Medium` / `High` | item labels (regex; add your UI language labels if needed) |
| `GEMINI_UI_SELECTED_RE` | `Selected` | prefix of the checked item, if the UI puts one in the name |
| `GEMINI_EFFORT_LOG` | `effort.log` next to the code | one line per session |
| `GEMINI_PICKER_WAIT_S` | `8` | the model guard polls a late-drawn picker this long (no false model-drop notice) |

## Log / state
- `effort.log`: `<time> <before> -> <after> target=... ok=... secs=... model=...`
- `/tmp/gemini_model_state.json`: `effort`, `effort_before`, `effort_target`, `effort_t`.
- Bridge stderr: `[effort] low -> medium (target medium, 1.3 s)`.

## By hand
`python3 src/gemini_model.py --effort-check` prints the level,
`python3 src/gemini_model.py --effort low` sets one (hold the bridge lock
while the proxy is running).

## Turn off
`GEMINI_WEB_EFFORT=off` in the env file; the next request picks it up.

## Per request
The level comes per request from the client (`reasoning_effort`, see
`docs/MODELS.md`): low / medium / high, `none` / `minimal` -> low, `xhigh` /
`max` / `ultra` -> high. Not sent -> `GEMINI_WEB_EFFORT` (default medium).
Works on all 3 models (Lite has the control too).
