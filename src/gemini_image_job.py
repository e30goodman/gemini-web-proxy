#!/usr/bin/python3
"""gemini_image_job.py - send an IMAGE + prompt to Gemini Web through the real Firefox on :2
(gemini-pump profile, same lock as gemini_atspi_bridge) and save the generated image(s).
usage: /usr/bin/python3 gemini_image_job.py <image.png[,image2.png,...]> <prompt.txt> <out_dir> [--probe] [--timeout 400]
(several images: comma-separated, pasted one after another in that order) [claude] 2026-11-04
[claude] 2026-11-03"""
import sys, os, time, fcntl, subprocess, glob, shutil, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import gemini_atspi_bridge as B
D = B.DISPLAY
def shot(path):
    subprocess.run(['import', '-window', 'root', '-display', D, path], timeout=30)
# ---- [claude] 2026-11-08 SELF-HEAL (author: 'Gemini gave the picture, the stand never got it'). Job crossbow_168230: Gemini drew the picture (chat
# d1169201c108ebc9), but at 21:06 ANOTHER user of the shared Firefox on :2 navigated it to claude.ai/login / tailscale / deepseek; the grab clicked into
# that page ('viewer toolbar not found') for 6 min and the job died; the next job found Xvfb :2 dead the same way. Now: our chat URL is read from the
# profile's history, and every grab round first checks (1) the display + Firefox are alive (else restarted), (2) the browser still shows OUR chat
# (else it is sent back to the chat URL). The chat URL is also printed / saved so a lost picture can always be fetched by hand.
PLACES_DIR = B.PROFILE
CHAT = {'url': '', 'heals': 0}
def visits_since(t0, limit=10):
    import sqlite3, tempfile
    d = tempfile.mkdtemp(prefix='pl_')
    try:
        for n in ('places.sqlite', 'places.sqlite-wal'):
            src = os.path.join(PLACES_DIR, n)
            if os.path.exists(src): shutil.copy(src, os.path.join(d, n))
        c = sqlite3.connect(os.path.join(d, 'places.sqlite'))
        rows = c.execute('select p.url, v.visit_date from moz_historyvisits v join moz_places p on p.id=v.place_id '
                         'where v.visit_date >= ? order by v.visit_date desc limit ?', (int((t0 - 3) * 1e6), limit)).fetchall()
        c.close(); return [r[0] for r in rows]
    except Exception as ex:
        print('[job] history read failed', ex, file=sys.stderr); return []
    finally:
        shutil.rmtree(d, ignore_errors=True)
def chat_id(url):
    m = re.search(r'gemini\.google\.com/app/([0-9a-f]{8,})', url or '')
    return m.group(1) if m else ''
def display_alive():
    try:
        return B.xvfb_running() and B.sh(['xdotool', 'getdisplaygeometry'], timeout=10).returncode == 0
    except Exception:
        return False
def goto(url):
    try:
        names = []
        for wid in B.sh(['xdotool', 'search', '--class', 'firefox']).stdout.split():
            nm = B.sh(['xdotool', 'getwindowname', wid]).stdout.strip()
            if 'Firefox' in nm: names.append(wid)
        if not names: return False
        B._activate(names[-1])
        B.sh(['xdotool', 'key', '--clearmodifiers', 'Escape']); time.sleep(0.3)
        B.sh(['xdotool', 'key', '--clearmodifiers', 'ctrl+l']); time.sleep(0.5)
        B.sh(['xdotool', 'type', '--clearmodifiers', '--delay', '15', url], timeout=60); time.sleep(0.4)
        B.sh(['xdotool', 'key', '--clearmodifiers', 'Return']); time.sleep(9)
        return True
    except Exception as ex:
        print('[job] goto failed', ex, file=sys.stderr); return False
def self_heal(t_start):
    """True = something was repaired (the caller grabs again at once)"""
    fixed = False
    if not display_alive():
        print('[job] HEAL: display :2 / Firefox is dead - restarting', file=sys.stderr)
        try: B.ensure_running(); time.sleep(3)
        except Exception as ex: print('[job] HEAL restart failed', ex, file=sys.stderr)
        fixed = True
    rows = visits_since(t_start)
    if not CHAT['url']:
        for u in rows:
            if chat_id(u): CHAT['url'] = u; print('[job] chat url', u, file=sys.stderr); break
    if not CHAT['url'] or CHAT['heals'] >= 8: return fixed
    newest = rows[0] if rows else ''
    cid = chat_id(CHAT['url'])
    foreign = bool(newest) and (('gemini.google.com' not in newest) or (chat_id(newest) and chat_id(newest) != cid))
    if fixed or foreign:
        CHAT['heals'] += 1
        print('[job] HEAL: the browser left our chat (%s) -> back to %s' % (newest[:70] or 'display was dead', CHAT['url']), file=sys.stderr)
        goto(CHAT['url']); fixed = True
    return fixed
def clip_image(path):
    env = dict(os.environ, DISPLAY=D)
    return subprocess.Popen(['xclip', '-selection', 'clipboard', '-t', 'image/png', '-i', path], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
def paste_text(text):
    env = dict(os.environ, DISPLAY=D)
    p = subprocess.Popen(['xclip', '-selection', 'clipboard'], env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.stdin.write(text.encode()); p.stdin.close(); time.sleep(0.3)
    B.sh(['xdotool', 'key', '--clearmodifiers', 'ctrl+v'], timeout=15)
    time.sleep(1.0); p.kill(); p.wait()
def main():
    a = sys.argv[1:]; probe = '--probe' in a; a = [x for x in a if x != '--probe']
    timeout = 400
    if '--timeout' in a: i = a.index('--timeout'); timeout = int(a[i+1]); del a[i:i+2]
    img, prompt_file, out = a[0], a[1], a[2]; os.makedirs(out, exist_ok=True)
    prompt = open(prompt_file).read()
    lock = open(B.BRIDGE_LOCK, 'w'); fcntl.flock(lock, fcntl.LOCK_EX)
    wid = B.ensure_running(); time.sleep(2)
    B.sh(['xdotool', 'key', '--clearmodifiers', 'Escape']); time.sleep(0.3)      # close Firefox popups (translations etc.)
    B.new_chat_session(wid)
    try:                                                                       # [claude] 2026-11-13 model guard
        import gemini_model; gemini_model.ensure_model(); gemini_model.ensure_effort()   # model + effort guard
    except Exception as ex:
        print('[job] model guard failed: %s' % ex, file=sys.stderr)
    B.sh(['xdotool', 'key', '--clearmodifiers', 'Escape']); time.sleep(0.3)
    def focus_entry():
        B._activate(wid)
        e = B.find_real_entry()
        if e is not None:
            try: e.queryComponent().grabFocus(); time.sleep(0.3)
            except Exception: pass
        return e
    entry = focus_entry()
    B.sh(['xdotool', 'key', '--clearmodifiers', 'ctrl+a']); B.sh(['xdotool', 'key', '--clearmodifiers', 'Delete'])
    for one in [x for x in img.split(',') if x]:              # 1) every image, in order
        c = clip_image(one); time.sleep(0.4)
        B.sh(['xdotool', 'key', '--clearmodifiers', 'ctrl+v'], timeout=15)
        time.sleep(3)
        c.kill(); c.wait()                                   # release the clipboard before the next paste
        subprocess.run(['pkill', '-u', str(os.getuid()), '-x', 'xclip'])
        time.sleep(0.6)
        focus_entry()
    entry = focus_entry()                                    # 2) then the text
    paste_text(prompt); time.sleep(1.0)
    shot(os.path.join(out, 'step1_attached.png'))
    print('[job] text len in field', B.field_text_len(entry), file=sys.stderr)
    if probe:
        print('[job] probe stop before Enter', file=sys.stderr); return
    # ---- send, wait for the generated image, click Gemini's own download button ----
    DL = os.environ.get('GEMINI_DOWNLOAD_DIR', os.path.expanduser('~/gemini_downloads')); os.makedirs(DL, exist_ok=True)
    before = set(os.listdir(DL))
    import pyatspi
    def walk(node, acc, depth=0):
        if depth > 60: return
        try: role = node.getRoleName(); name = node.name or ''
        except Exception: return
        acc.append((role, name, node))
        try: n = node.childCount
        except Exception: n = 0
        for i in range(n):
            try: walk(node.getChildAtIndex(i), acc, depth + 1)
            except Exception: pass
    def firefox():
        for a in pyatspi.Registry.getDesktop(0):
            if a.name == 'Firefox': return a
    # Download-button label(s) in the Gemini UI (English default; override via env).
    KEYS = tuple(k.strip() for k in os.environ.get('GEMINI_UI_DOWNLOAD_KEYS', 'download').split(',') if k.strip())
    base = []; walk(firefox(), base)
    baseline = {n for r, n, nd in base if any(k in (n or '').lower() for k in KEYS)}
    print('[job] baseline download-like names:', sorted(baseline), file=sys.stderr)
    def hover_generated(acc):
        # [claude] 2026-11-04: with 2 attached pictures the answer image lands below the fold and Gemini shows its
        # download button only on hover -> scroll the newest big image into view and put the mouse on it
        imgs = []
        for r, n, nd in acc:
            if r in ('image', 'graphic'):
                try: c = nd.queryComponent(); e = c.getExtents(pyatspi.DESKTOP_COORDS)
                except Exception: continue
                if e.width >= 250 and e.height >= 150: imgs.append((e.y, id(nd), c))
        if not imgs:
            # fallback by mouse (AT-SPI often gives no image extents under Xvfb): wheel the chat down over the answer area,
            # hover the answer image; every 3rd try also click it (Gemini's viewer has the same full-size download button)
            hover_generated.n = getattr(hover_generated, 'n', 0) + 1
            B.sh(['xdotool', 'mousemove', '640', '330'])
            for _ in range(8): B.sh(['xdotool', 'click', '5']); time.sleep(0.08)
            time.sleep(0.8)
            B.sh(['xdotool', 'mousemove', '640', '300'])
            if hover_generated.n % 3 == 0:
                B.sh(['xdotool', 'click', '1']); print('[job] clicked the answer image (viewer)', file=sys.stderr)
            return True
        _, _, c = sorted(imgs, key=lambda t: t[0])[-1]
        try: c.scrollTo(pyatspi.SCROLL_ANYWHERE)
        except Exception: pass
        time.sleep(0.6)
        try: e = c.getExtents(pyatspi.DESKTOP_COORDS)
        except Exception: return False
        B.sh(['xdotool', 'mousemove', str(e.x + e.width // 2), str(e.y + e.height // 2)])
        return True
    # [claude] 2026-11-05 (author: 'sometimes Gemini glitches and answers "I am just a language model, I can not help" - make an auto-check;
    # no picture + that text = the trigger to open a NEW chat and send the request again'). OCR of the screen (eng+rus) is cheap, the AT-SPI walk is not.
    REFUSAL = ('language model', "can't help", 'cannot help', "can't assist", 'cannot assist',
               'unable to help', "i'm not able to help", 'not able to help')
    def refused_on_screen():
        p = '/tmp/gemini_refuse_check.png'
        try:
            shot(p)
            txt = subprocess.run(['tesseract', p, 'stdout', '-l', os.environ.get('GEMINI_OCR_LANG', 'eng')], capture_output=True, text=True, timeout=60).stdout
        except Exception as ex:
            print('[job] refusal check failed', ex, file=sys.stderr); return ''
        low = ' '.join(txt.lower().split())
        for k in REFUSAL:
            if k in low: return k
        return ''
    B.sh(['xdotool', 'key', '--clearmodifiers', 'Return'])
    t0 = time.time(); seen = set(); clicked = False; hovered_at = 0; last_refuse_check = 0
    # [claude] 2026-11-04 author's recipe, by screen only (the AT-SPI walk of the Gemini page takes minutes per pass):
    # wait, then every 15 s hover the newest answer picture and click its 'download full size' button (top-right)
    last_grab = 0
    while time.time() - t0 < timeout:
        time.sleep(3)
        new = [f for f in set(os.listdir(DL)) - before if not f.endswith('.part')]
        if new:
            time.sleep(1.5)
            for f in new:
                shutil.move(os.path.join(DL, f), os.path.join(out, f)); print('[job] SAVED', os.path.join(out, f))
            shot(os.path.join(out, 'step2_done.png')); return
        if time.time() - t0 > 15 and time.time() - last_refuse_check > 10:
            last_refuse_check = time.time(); hit = refused_on_screen()
            if hit:
                shot(os.path.join(out, 'step2_refused.png'))
                print('[job] REFUSED: Gemini answered with text (%r), no picture -> exit 4, the caller opens a new chat and retries' % hit, file=sys.stderr)
                sys.exit(4)
        if time.time() - t0 > 20 and time.time() - last_grab > 12:
            try:
                if self_heal(t0): last_grab = 0   # repaired: grab at once
            except Exception as ex: print('[job] self_heal error', ex, file=sys.stderr)
            # [claude] 2026-11-05 (author: 'the viewer is the MAIN way to download'): open the answer in Gemini's viewer and press its
            # download icon (gemini_viewer_download.py). The old hover-button grab stays only as the fallback on every 3rd try.
            grab_n = globals().get('_grab_n', 0) + 1; globals()['_grab_n'] = grab_n
            script = (os.environ.get('GEMINI_GRAB_SCRIPT', str(Path(__file__).parent.parent / 'scripts' / 'gemini_grab_answer.py')) if grab_n % 3 == 0
              else os.environ.get('GEMINI_VIEWER_SCRIPT', str(Path(__file__).parent.parent / 'scripts' / 'gemini_viewer_download.py')))
            r_ = subprocess.run(['/usr/bin/python3', script], env=dict(os.environ, DISPLAY=':2'), capture_output=True, text=True)
            print('[job] grab(%s):' % os.path.basename(script), (r_.stdout + r_.stderr).strip()[-200:], file=sys.stderr); last_grab = time.time()
    shot(os.path.join(out, 'step2_timeout.png'))
    try: open(os.path.join(out, 'chat_url.txt'), 'w').write(CHAT['url'])
    except Exception: pass
    print('[job] TIMEOUT (screen grab) chat url: %s (open it, the picture may be there)' % (CHAT['url'] or 'unknown'), file=sys.stderr); sys.exit(3)
    while time.time() - t0 < timeout:
        time.sleep(4)
        acc = []; walk(firefox(), acc)
        for role, name, node in acc:
            low = name.lower()
            if role in ('push button', 'button', 'link', 'menu item') and any(k in low for k in KEYS) and name not in seen:
                seen.add(name); print('[job] candidate', role, repr(name), file=sys.stderr)
        cand = [(r, n, nd) for r, n, nd in acc if r in ('push button', 'button') and any(k in n.lower() for k in KEYS) and n not in baseline]
        if not cand and not clicked and time.time() - t0 > 12 and time.time() - hovered_at > 8:
            # author's recipe 2026-11-04: hover the answer picture -> its 3 buttons appear (top-right) -> 'Download full size'
            r_ = subprocess.run(['/usr/bin/python3', os.environ.get('GEMINI_GRAB_SCRIPT', str(Path(__file__).parent.parent / 'scripts' / 'gemini_grab_answer.py')), '--hover-only'], env=dict(os.environ, DISPLAY=':2'), capture_output=True, text=True)
            print('[job] screen hover:', (r_.stdout + r_.stderr).strip()[-160:], file=sys.stderr)
            hovered_at = time.time(); time.sleep(0.8); acc = []; walk(firefox(), acc)
            cand = [(r, n, nd) for r, n, nd in acc if r in ('push button', 'button') and any(k in n.lower() for k in KEYS) and n not in baseline]
        if False:
                hovered_at = time.time(); print('[job] hovered the newest image', file=sys.stderr)
                time.sleep(1.0); acc = []; walk(firefox(), acc)
                cand = [(r, n, nd) for r, n, nd in acc if r in ('push button', 'button') and any(k in n.lower() for k in KEYS) and n not in baseline]
        if cand and not clicked:
            r, n, nd = cand[-1]
            try:
                act = nd.queryAction(); act.doAction(0); clicked = True
                print('[job] clicked', repr(n), 'after %.0fs' % (time.time() - t0), file=sys.stderr)
            except Exception as ex:
                print('[job] click failed', ex, file=sys.stderr)
        if clicked:
            new = [f for f in set(os.listdir(DL)) - before if not f.endswith('.part')]
            if new:
                for f in new:
                    shutil.move(os.path.join(DL, f), os.path.join(out, f)); print('[job] SAVED', os.path.join(out, f))
                shot(os.path.join(out, 'step2_done.png')); return
    shot(os.path.join(out, 'step2_timeout.png'))
    names = sorted({(r, n) for r, n, nd in acc if r in ('push button', 'button', 'link', 'image', 'graphic') and n})
    print('[job] TIMEOUT. buttons/images seen:', file=sys.stderr)
    for r, n in names[-60:]: print('   ', r, repr(n[:80]), file=sys.stderr)
    sys.exit(3)

main()
