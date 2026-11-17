#!/usr/bin/env python3
# Gemini model guard: finds the mode picker by its accessible name (not fixed
# coordinates), detects a reset model, switches to the target and verifies. Used
# by gemini_atspi_bridge (every session, fresh and continue) and gemini_image_job.
# Target: env GEMINI_WEB_MODEL = a MODELS key (lite / hydrogen / argon, set per
# request by the proxy) or a menu name; default Hydrogen. See docs/MODELS.md.
# Also the reasoning-level (effort) guard, see docs/RESILIENCE.md.
# CLI: gemini_model.py [--check] [target] | --effort-check | --effort [low|medium|high]
import os, re, subprocess, sys, time

TARGET = os.environ.get('GEMINI_WEB_MODEL', 'Hydrogen')
DISPLAY = os.environ.get('DISPLAY', ':2')

# MODELS: the proxy picks the model per request (client model id -> key, see
# server.WEB_MODELS). 'item' finds the menu item / the picker label by the model
# name (the same in every UI language); 'desc' is the item's subtitle - a limited
# item loses the model name and keeps only '<desc> Limit resets ...', so the limit
# is matched by 'desc'. Subtitles default to English, override per model with
# GEMINI_UI_<KEY>_DESC_RE for another UI language.
# strict: a missing/locked item is an error to the client, not a silent other
# model (Hydrogen is the default and keeps the fail-open behaviour).
MODELS = {
    'hydrogen': {'name': 'Hydrogen', 'item': r'\bHydrogen\b',
                 'desc': os.environ.get('GEMINI_UI_HYDROGEN_DESC_RE', r'Everyday'), 'strict': False},
    'lite': {'name': 'Flash-Lite', 'item': r'Flash[- ]?Lite',
             'desc': os.environ.get('GEMINI_UI_LITE_DESC_RE', r'Fast answer|Quick answer'), 'strict': True},
    'argon': {'name': 'Argon', 'item': r'\bArgon\b',
              'desc': os.environ.get('GEMINI_UI_ARGON_DESC_RE', r'Code (and|&) analysis|Coding'), 'strict': True},
}
# a locked (no subscription) item: upsell words in its name or a disabled state
LOCKED_RE = re.compile(os.environ.get('GEMINI_UI_LOCKED_RE',
    r'(Upgrade|Subscribe|Get Google AI|Google AI (Pro|Ultra)|Unlock)'), re.I)
UNAVAILABLE_TEXT = os.environ.get('GEMINI_MODEL_UNAVAILABLE_TEXT', '{model} is not available on this account')
LAST_FAIL = ''  # why the last ensure_model failed: limit | locked | missing | verify | picker


def spec(target=None):
    '''(key, spec) for a model key ('lite') or menu name ('Flash-Lite'); unknown
    names get an ad-hoc spec that matches the name literally.'''
    t = (target or TARGET or 'Hydrogen').strip()
    if t.lower() in MODELS:
        return t.lower(), MODELS[t.lower()]
    for k, sp in MODELS.items():
        if t.lower() == sp['name'].lower() or re.search(sp['item'], t, re.I):
            return k, sp
    return t.lower(), {'name': t, 'item': re.escape(t), 'desc': None, 'strict': True}


def unavailable_text(target=None):
    '''Plain text for the client when the asked model cannot be selected.'''
    return UNAVAILABLE_TEXT.format(model=spec(target)[1]['name'])
# UI labels are matched against the Gemini web UI. Defaults are English; if your
# Gemini UI is in another language, override with GEMINI_UI_PICKER_RE (a regex).
PICKER_RE = re.compile(os.environ.get('GEMINI_UI_PICKER_RE',
    r'^(Mode picker|Choose (a )?mode|Select mode)'), re.I)


def log(msg):
    print('[model] ' + msg, file=sys.stderr, flush=True)


def _x(*args, timeout=10):
    return subprocess.run(['xdotool', *args], env=dict(os.environ, DISPLAY=DISPLAY),
                          capture_output=True, text=True, timeout=timeout)


def _firefox():
    import pyatspi
    for a in pyatspi.Registry.getDesktop(0):
        try:
            if a and a.name == 'Firefox':
                return a
        except Exception:
            pass
    return None


def _find(pred, root, limit=200000):
    """Depth-first search, last match wins for the picker (it sits at the end of the page)."""
    stack, seen, hit = [root], 0, []
    while stack and seen < limit:
        n = stack.pop(); seen += 1
        try:
            if pred(n):
                hit.append(n)
            for i in range(n.childCount - 1, -1, -1):
                c = n.getChildAtIndex(i)
                if c is not None:
                    stack.append(c)
        except Exception:
            continue
    return hit


def _center(n):
    import pyatspi
    e = n.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
    return e.x + e.width // 2, e.y + e.height // 2, e.width, e.height


def _visible(n):
    try:
        x, y, w, h = _center(n)
        return w > 0 and h > 0 and y > 0
    except Exception:
        return False


def _coll(roles):
    """Nodes of these roles via the AT-SPI Collection interface (one D-Bus call,
    ~10 ms) instead of a node-by-node walk (~1 s); the walk is the fallback."""
    import pyatspi
    ff = _firefox()
    if ff is None:
        return []
    try:
        ids = {pyatspi.ROLE_PUSH_BUTTON if r in ('button', 'push button')
               else getattr(pyatspi, 'ROLE_' + r.upper().replace(' ', '_')) for r in roles}
        c = ff.queryCollection()
        rule = c.createMatchRule(pyatspi.StateSet(), c.MATCH_NONE, {}, c.MATCH_NONE,
                                 list(ids), c.MATCH_ANY, [], c.MATCH_NONE, False)
        return list(c.getMatches(rule, c.SORT_ORDER_CANONICAL, 0, True))
    except Exception as e:
        log('collection search failed (%s), walking the tree' % e)
        return _find(lambda n: n.getRoleName() in roles, ff)


def find_picker():
    hits = [n for n in _coll(('button', 'push button')) if n.name and PICKER_RE.match(n.name) and _visible(n)]
    return hits[-1] if hits else None


def current_model():
    p = find_picker()
    if p is None:
        return None
    m = re.search(os.environ.get('GEMINI_UI_CURRENT_RE', r'(using|current(ly)?:?)\s*(.+)$'), p.name, re.I)
    return (m.group(3) if m else p.name).strip()


def _click(n, fast=False):
    x, y, _, _ = _center(n)
    if fast:   # one xdotool call
        _x('mousemove', '--sync', str(x), str(y), 'sleep', '0.05', 'click', '1')
        return
    _x('mousemove', '--sync', str(x), str(y))
    time.sleep(0.25)
    _x('click', '1')


def _is(sp, text):
    return bool(text and re.search(sp['item'], text, re.I))


def _menu_items():
    ff = _firefox()
    return [n for n in _find(lambda n: n.getRoleName() in _MENU_ROLES and n.name, ff) if _visible(n)] if ff else []


def _enabled(n):
    import pyatspi
    try:
        st = n.getState()
        return st.contains(pyatspi.STATE_ENABLED) or st.contains(pyatspi.STATE_SENSITIVE)
    except Exception:
        return True


def _limit_of(sp, names):
    """Limit text of THIS model's item (matched by name or subtitle), '' if none."""
    for nm in reversed(names):
        if LIMIT_ITEM_RE.search(nm) and (_is(sp, nm) or (sp.get('desc') and re.search(sp['desc'], nm, re.I))):
            return nm.strip()[:160]
    return ''


def ensure_model(target=None, tries=3):
    """Returns True when the picker shows the target model. target = model key
    ('hydrogen', 'lite', 'argon') or menu name; default TARGET (env GEMINI_WEB_MODEL).
    On False, LAST_FAIL says why (limit / locked / missing / verify / picker)."""
    global LAST_LIMIT, LAST_FAIL
    key, sp = spec(target)
    name = sp['name']
    LAST_FAIL = ''
    fails = []
    # the fast (Collection) lookup can run before a fresh chat has drawn the picker
    # -> poll it for GEMINI_PICKER_WAIT_S before using up attempts
    deadline = time.time() + float(os.environ.get('GEMINI_PICKER_WAIT_S', '8'))
    attempt = 0
    while attempt < tries:
        p = find_picker()
        if p is None and time.time() < deadline:
            time.sleep(0.3)
            continue
        attempt += 1
        if p is None:
            log('picker not found (attempt %d)' % attempt)
            fails.append('picker')
            time.sleep(1.5)
            continue
        cur = current_model() or '?'
        if _is(sp, cur):
            log('ok: %s' % cur)
            return True
        log('model is %r, switching to %s (attempt %d)' % (cur, name, attempt))
        _click(p)
        item, names = None, []
        for _ in range(12):
            time.sleep(0.25)
            items = _menu_items()
            names = [n.name for n in items]
            hit = [n for n in items if _is(sp, n.name)]
            if hit:
                item = hit[-1]
                break
        if item is None:
            # a limited model's item loses its name and shows the limit text - keep
            # it; only the asked model's item counts (matched by its subtitle)
            lim = _limit_of(sp, names)
            if lim:
                LAST_LIMIT = lim
                log('limit on the menu: %s' % LAST_LIMIT)
                fails.append('limit')
            else:
                fails.append('missing' if names else 'picker')
            log('menu item %s not found (menu: %s)' % (name, ' | '.join(names)[:300]))
            _x('key', '--clearmodifiers', 'Escape')
            time.sleep(0.5)
            continue
        if not _enabled(item) or LOCKED_RE.search(item.name):
            log('menu item %s is locked: %r' % (name, item.name))
            fails.append('locked')
            _x('key', '--clearmodifiers', 'Escape')
            time.sleep(0.5)
            break   # no point in clicking it again
        _click(item)
        time.sleep(1.0)
        now = current_model() or '?'
        if _is(sp, now):
            log('switched: %s' % now)
            return True
        log('verify failed: picker shows %r' % now)
        fails.append('verify')
        _close_menu()   # an upsell dialog may be open on top
        time.sleep(0.5)
    now = current_model() or ''
    if _is(sp, now):   # page settled late - no false drop
        log('ok on re-check: %s' % now)
        return True
    for why in ('limit', 'locked', 'missing', 'verify', 'picker'):
        if why in fails:
            LAST_FAIL = why
            break
    log('FAILED to select %s (%s), current model %r' % (name, LAST_FAIL or '?', now))
    return False


def unavailable(target=None):
    """True when the last ensure_model failure means the asked model cannot be
    used on this account (strict models only; a limit is not this)."""
    sp = spec(target)[1]
    return bool(sp.get('strict')) and LAST_FAIL in ('missing', 'locked', 'verify')


def item_limit(target=None):
    """Open the picker and read the limit text of THIS model's item ('' = not
    limited). Used when a non-default model is on screen and a page banner says
    'limit' - the banner may belong to another model."""
    sp = spec(target)[1]
    p = find_picker()
    if p is None:
        return ''
    _click(p, fast=True)
    names = []
    try:
        hit = _wait(lambda s: LIMIT_ITEM_RE.search(s) or _is(sp, s), _MENU_ROLES)
        names = [n.name for n in hit]
    finally:
        _close_menu()
    return _limit_of(sp, names)


LAST_LIMIT = ''  # limit text seen on the model menu during ensure_model
# a limited model's menu item shows e.g. 'Limit resets Nov 16, 11:38 AM' instead of its name
LIMIT_ITEM_RE = re.compile(os.environ.get('GEMINI_UI_LIMIT_ITEM_RE',
                                          r'(limit (resets|reached|will reset)|resets (at|on|in) )'), re.I)


def limit_info():
    """Best limit text: the menu item (has the date) else the page banner."""
    return LAST_LIMIT or limit_banner()


def limit_banner():
    """The 'Limit reached. Resets at 19:38' banner text from the page, or ''."""
    ff = _firefox()
    if ff is None:
        return ''
    hit = _find(lambda n: 'Text' in n.get_interfaces()
                and re.search(os.environ.get('GEMINI_UI_LIMIT_RE', r'(limit reached)'),
                              n.queryText().getText(0, -1) or '', re.I)
                and len(n.queryText().getText(0, -1) or '') < 200, ff)
    if not hit:
        return ''
    try:
        return hit[-1].queryText().getText(0, -1).strip()[:80]
    except Exception:
        return ''


def write_state(ok, path='/tmp/gemini_model_state.json'):
    """Model status for the proxy (notice to the client on a drop)."""
    import json
    cur = spec(TARGET)[1]['name'] if ok else current_model()
    if not ok and not cur:
        # picker not found (page still loading after a cold start) - unknown is not a
        # drop; keep the previous state, no false notice to Hermes
        log('state not written: model unknown')
        return None
    st = {'ok': bool(ok), 'model': cur, 'target': spec(TARGET)[1]['name'], 't': time.time(),
          'limit': '' if ok else limit_info()}
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log('state write failed: %s' % e)
    return st


# EFFORT GUARD: Gemini web resets the reasoning level (the 'Reasoning level'
# submenu of the mode picker) to Low now and then (page loads); answers on low are
# worse. Every bridge session reads it and sets the target. Target: env
# GEMINI_WEB_EFFORT = low | medium (default) | high | off (no guard). Fast path:
# the picker label names the level when it is not low ('Hydrogen Medium').
# UI labels default to English; override the regexes for another UI language.
EFFORT = os.environ.get('GEMINI_WEB_EFFORT', 'medium').strip().lower()
EFFORT_LOG = os.environ.get('GEMINI_EFFORT_LOG',
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'effort.log'))
EFFORT_MENU_RE = re.compile(os.environ.get('GEMINI_UI_EFFORT_MENU_RE',
    r'^(Reasoning level|Thinking level|Reasoning effort|Thinking)'), re.I)
EFFORT_LEVELS = {'low': os.environ.get('GEMINI_UI_EFFORT_LOW_RE', r'Low'),
                 'medium': os.environ.get('GEMINI_UI_EFFORT_MEDIUM_RE', r'Medium'),
                 'high': os.environ.get('GEMINI_UI_EFFORT_HIGH_RE', r'High')}
_SEL = os.environ.get('GEMINI_UI_SELECTED_RE', r'Selected')
_EFFORT_ITEM_RE = re.compile(r'^(?:(?:%s) )?(%s)' % (_SEL, '|'.join(EFFORT_LEVELS.values())), re.I)
_SELECTED_RE = re.compile(r'^(%s)\b' % _SEL, re.I)
_MENU_ROLES = ('radio menu item', 'menu item', 'check menu item')


def _level(name, anchored=True):
    for k, rx in EFFORT_LEVELS.items():
        if re.search((r'^(?:(?:%s) )?(%s)' % (_SEL, rx)) if anchored else (r'\b(%s)' % rx), name or '', re.I):
            return k
    return None


def label_effort():
    """Level named on the picker label ('Hydrogen Medium'), None when not named (low)."""
    return _level(current_model() or '', anchored=False)


def _wait(pred, roles, secs=2.5):
    """Poll until visible nodes of these roles match pred (popups render late)."""
    end = time.time() + secs
    while True:
        hit = [n for n in _coll(roles) if n.name and pred(n.name) and _visible(n)]
        if hit or time.time() > end:
            return hit
        time.sleep(0.1)


def _effort_items():
    """Open picker -> reasoning submenu; returns (items, current level) or (None, None)."""
    import pyatspi
    p = find_picker()
    if p is None:
        return None, None
    _click(p, fast=True)
    sub = _wait(lambda s: EFFORT_MENU_RE.match(s), ('menu', 'menu item'))
    if not sub:
        return None, None
    _click(sub[-1], fast=True)
    items = _wait(lambda s: _EFFORT_ITEM_RE.match(s), _MENU_ROLES)
    cur = None
    for n in items:
        try:
            if n.getState().contains(pyatspi.STATE_CHECKED) or _SELECTED_RE.match(n.name):
                cur = _level(n.name)
        except Exception:
            pass
    return items, cur


def _close_menu():
    _x('key', '--clearmodifiers', 'Escape')
    time.sleep(0.1)
    _x('key', '--clearmodifiers', 'Escape')


def current_effort():
    lv = label_effort()
    if lv:
        return lv
    items, cur = _effort_items()
    _close_menu()
    return cur if items else None


def ensure_effort(target=None, tries=2):
    """Returns {'before','after','ok','secs'}; never raises, never blocks the request.
    Missing control (model without it, page change) -> logged, ok=None."""
    target = (target or EFFORT).lower()
    t0 = time.time()
    res = {'target': target, 'before': None, 'after': None, 'ok': None, 'secs': 0.0}
    if target in ('off', '0', 'none', ''):
        return res
    if target not in EFFORT_LEVELS:
        log('effort: unknown GEMINI_WEB_EFFORT=%r, guard off' % target)
        return res
    try:
        if label_effort() == target:          # fast path, no menu
            res.update(before=target, after=target, ok=True)
        for attempt in range(1, tries + 1):
            if res['ok']:
                break
            items, cur = _effort_items()
            if not items:
                _close_menu()
                log('effort: control not found (model %s), continuing' % (current_model() or '?'))
                break
            if res['before'] is None:
                res['before'] = cur
            if cur == target:
                _close_menu()
                res['after'], res['ok'] = cur, True
                break
            want = [n for n in items if _level(n.name) == target]
            if not want:
                _close_menu()
                log('effort: no %s item in %s' % (target, [n.name for n in items]))
                break
            log('effort RESET detected: %s -> switching to %s (attempt %d)' % (cur, target, attempt))
            _click(want[-1], fast=True)
            # verify: the label names medium/high; low (unnamed) needs the menu
            now, end = None, time.time() + 2.0
            while target != 'low' and time.time() < end:
                now = label_effort()
                if now == target:
                    break
                time.sleep(0.1)
            if now != target:
                _close_menu()
                _, now = _effort_items()
                _close_menu()
            res['after'] = now
            res['ok'] = now == target
            if not res['ok']:
                log('effort verify failed: shows %s' % now)
    except Exception as e:
        _close_menu()
        log('effort guard error: %s' % e)
    res['secs'] = round(time.time() - t0, 2)
    log('effort %s -> %s (target %s, %.1f s)' % (res['before'], res['after'], target, res['secs']))
    _effort_record(res)
    return res


def _effort_record(res, path='/tmp/gemini_model_state.json'):
    """Effort into the model state file + one line in effort.log."""
    import json
    try:
        st = json.load(open(path, encoding='utf-8'))
    except Exception:
        st = {}
    st.update({'effort': res['after'], 'effort_before': res['before'],
               'effort_target': res['target'], 'effort_t': time.time()})
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log('effort state write failed: %s' % e)
    try:
        with open(EFFORT_LOG, 'a', encoding='utf-8') as f:
            f.write('%s %s -> %s target=%s ok=%s secs=%.2f model=%s\n' % (
                time.strftime('%Y-%m-%d %H:%M:%S'), res['before'], res['after'], res['target'],
                res['ok'], res['secs'], st.get('model')))
    except Exception:
        pass


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    t = args[0] if args else TARGET
    if '--check' in sys.argv:
        print(current_model())
        sys.exit(0)
    if '--effort-check' in sys.argv:
        print(current_effort())
        sys.exit(0)
    if '--effort' in sys.argv:         # gemini_model.py --effort [low|medium|high]
        r = ensure_effort(args[0] if args else None)
        print(r)
        sys.exit(0 if r['ok'] else 1)
    sys.exit(0 if ensure_model(t) else 1)
