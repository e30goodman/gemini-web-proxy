#!/usr/bin/env python3
"""Hover the newest Gemini answer image and click its 'download full size' button, by screen analysis (no AT-SPI).
usage: DISPLAY=:N gemini_grab_answer.py [--dry]   [claude] 2026-11-04 (author's recipe: hover the picture -> 3 buttons appear at its top-right -> click the download one)"""
import subprocess, sys, time, numpy as np
from PIL import Image
from scipy import ndimage
def shot():
    subprocess.run(['import','-window','root','/tmp/gga.png'],check=True); return np.asarray(Image.open('/tmp/gga.png').convert('RGB')).astype(int)
def sh(*a): subprocess.run(['xdotool',*a],check=False)
a=shot(); H,W=a.shape[:2]
bg=np.median(a[H//2-40:H//2+40, W-60:W-20].reshape(-1,3),0)            # chat background near the right edge
diff=np.abs(a-bg).sum(2)>18
x0,x1=int(W*0.17),int(W*0.50)                                            # the answer sits LEFT in the chat; my message is right-aligned
rows=[y for y in range(int(H*0.10),int(H*0.90)) if diff[y,x0:x1].mean()>0.85]
if not rows: print('no answer image rows'); sys.exit(2)
# the newest answer = the LAST block of consecutive rows
blocks=[]; st=rows[0]; pv=rows[0]
for y in rows[1:]:
    if y!=pv+1: blocks.append((st,pv)); st=y
    pv=y
blocks.append((st,pv)); top,bot=max(blocks,key=lambda b:(b[0], b[1]-b[0]))
if bot-top<60: top,bot=max(blocks,key=lambda b:b[1]-b[0])
yy=min(top+20,bot)
row=diff[yy]; left=x0
while left>0 and row[left-1:left].all(): left-=1   # walk left from the chat column, not from the sidebar
right=left
while right<W-1 and (row[right] or row[min(W-1,right+1):min(W,right+25)].any()): right+=1
vis_bot=min(bot,int(H*0.78))
cx,cy=(left+right)//2,(top+vis_bot)//2
print('answer image box',left,top,right,bot,'hover',cx,cy)
sh('mousemove',str(cx),str(cy)); time.sleep(1.3)
if '--hover-only' in sys.argv: sys.exit(0)
b=shot()
# the 3 round buttons appear near the image top-right: look for a cluster of light icon pixels there
reg=b[max(0,top-10):top+90, max(0,right-260):right+10]
light=(reg.sum(2)>500)
lb,nb=ndimage.label(light); boxes=ndimage.find_objects(lb)
icons=sorted([ (bx[1].start+bx[1].stop)//2 for bx in boxes if bx and (bx[0].stop-bx[0].start)>=8 ])
if not icons: print('no buttons seen'); sys.exit(3)
ys_=[ (bx[0].start+bx[0].stop)//2 for bx in boxes if bx and (bx[0].stop-bx[0].start)>=8 ]
bx_=max(right-260,0)+icons[-1]; by_=max(0,top-10)+int(np.median(ys_))
print('download button at',bx_,by_)
if '--dry' not in sys.argv:
    sh('mousemove',str(bx_),str(by_)); time.sleep(0.4); sh('click','1'); print('clicked')
