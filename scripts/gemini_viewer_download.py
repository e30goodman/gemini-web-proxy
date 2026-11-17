#!/usr/bin/env python3
"""MAIN way to take Gemini's generated picture: open the newest answer image in Gemini's viewer (a click on it) and press the
viewer's own download icon (top-right toolbar: share, copy, DOWNLOAD, undo, redo, Save). By screen analysis, no AT-SPI.
usage: DISPLAY=:N gemini_viewer_download.py [--dry]    [claude] 2026-11-05 (author: 'make the viewer the main way to download').
Why not the hover buttons (gemini_grab_answer.py, the old way): a tall answer picture does not fit the screen, the composer
bar covers its bottom, and the 'newest block' heuristic took the composer / footer strip -> clicks into nothing, 300 s timeout.
Here: scroll to the bottom, look only ABOVE the composer, click the picture, then the toolbar icon is always at the same place."""
import subprocess, sys, time, numpy as np
from PIL import Image
from scipy import ndimage
def shot(p='/tmp/gvd.png'):
    subprocess.run(['import', '-window', 'root', p], check=True)
    return np.asarray(Image.open(p).convert('RGB')).astype(int)
def sh(*a): subprocess.run(['xdotool', *a], check=False)

def viewer_open(a):
    """the viewer = a dark page with a toolbar of light round icons in the top-right corner and a big picture in the middle"""
    H, W = a.shape[:2]
    return find_download(a) is not None and a[int(H * 0.25):int(H * 0.60), :int(W * 0.12)].mean() < 70

def find_download(a):
    H, W = a.shape[:2]
    # [claude] 2026-11-08: the band is ONLY the toolbar row (y 0.11..0.205 H, x from 0.70 W: share, copy, DOWNLOAD, undo, redo, Save).
    # Before (0.25 H / 0.55 W) the top edge of a big picture (y ~0.23 H) and its frame were taken for icons, cl[2] became the COPY icon
    # (toast 'copied to clipboard', the viewer switched to its sketch mode) and no file ever landed (recovery of crossbow_168230).
    y0, y1 = int(H * 0.11), int(H * 0.205)
    x0 = int(W * 0.70)
    reg = a[y0:y1, x0:]
    light = reg.sum(2) > 520
    lb, nb = ndimage.label(ndimage.binary_dilation(light, iterations=2))
    boxes = [b for b in ndimage.find_objects(lb) if b is not None]
    cl = []
    for bx in boxes:
        h = bx[0].stop - bx[0].start; w = bx[1].stop - bx[1].start
        if 14 <= h <= 60 and 14 <= w <= 60:        # an icon (the 'Save' pill is wider)
            cl.append(((bx[1].start + bx[1].stop) // 2 + x0, (bx[0].start + bx[0].stop) // 2 + y0))
    cl.sort()
    return cl[2] if len(cl) >= 3 else None            # share, copy, DOWNLOAD

a = shot(); H, W = a.shape[:2]
if not viewer_open(a):
    # 1. the newest answer must be on the screen: scroll the chat to the very bottom
    sh('mousemove', str(int(W * 0.5)), str(int(H * 0.45)))
    for _ in range(25): sh('click', '5'); time.sleep(0.05)
    time.sleep(1.2); a = shot()
    bg = np.median(a[H // 2 - 40:H // 2 + 40, W - 60:W - 20].reshape(-1, 3), 0)
    diff = np.abs(a - bg).sum(2) > 18
    x0, x1 = int(W * 0.25), int(W * 0.55)             # the picture's column (the sidebar is left of 0.22, my message is right-aligned)
    top_lim, bot_lim = int(H * 0.12), int(H * 0.78)   # NEVER the composer / footer strip below 0.78 H
    rows = [y for y in range(top_lim, bot_lim) if diff[y, x0:x1].mean() > 0.85]
    if not rows: print('no answer image rows'); sys.exit(2)
    blocks = []; st = pv = rows[0]
    for y in rows[1:]:
        if y != pv + 1: blocks.append((st, pv)); st = y
        pv = y
    blocks.append((st, pv))
    top, bot = max(blocks, key=lambda b: b[1] - b[0])  # the tallest picture-like block above the composer (the newest, scrolled to)
    cx, cy = (x0 + x1) // 2, (top + bot) // 2
    print('answer picture rows %d..%d, click %d,%d' % (top, bot, cx, cy))
    if '--dry' in sys.argv: sys.exit(0)
    sh('mousemove', str(cx), str(cy)); time.sleep(0.8); sh('click', '1'); time.sleep(2.5)
    a = shot()
d = find_download(a)
if d is None:
    print('viewer toolbar not found'); sys.exit(3)
print('viewer download icon at %d,%d' % d)
if '--dry' not in sys.argv:
    sh('mousemove', str(d[0]), str(d[1])); time.sleep(0.5); sh('click', '1'); print('clicked')
