#!/usr/bin/env python3
"""Скан шапок актов: берёт каждый N-й кадр и вырезает строку с названием сельсовета.
   headers_scan.py <папка> <выход> [шаг] [y0 y1]"""
import sys, glob, os
from PIL import Image, ImageDraw
d, out = sys.argv[1], sys.argv[2]
step = int(sys.argv[3]) if len(sys.argv) > 3 else 6
y0, y1 = (float(sys.argv[4]), float(sys.argv[5])) if len(sys.argv) > 5 else (0.125, 0.185)
os.makedirs(out, exist_ok=True)
fs = sorted(glob.glob(os.path.join(d,'0*.jpg')))[::step]
CW, cols, rows = 880, 3, 8
def strip(p):
    im = Image.open(p); w,h = im.size
    c = im.crop((int(w*0.28), int(h*y0), int(w*0.97), int(h*y1)))
    r = CW/c.size[0]; return c.resize((CW, int(c.size[1]*r)))
per = cols*rows
for k in range(0, len(fs), per):
    grp = fs[k:k+per]; cells=[strip(p) for p in grp]
    CH = max(c.size[1] for c in cells)+24
    canv = Image.new('RGB',(CW*cols+8*(cols-1), CH*rows),'white'); dr=ImageDraw.Draw(canv)
    for i,(p,c) in enumerate(zip(grp,cells)):
        x=(i%cols)*(CW+8); y=(i//cols)*CH
        dr.text((x+5,y+4), os.path.basename(p)[:4], fill='red'); canv.paste(c,(x,y+20))
    canv.save(f"{out}/h{os.path.basename(grp[0])[:4]}-{os.path.basename(grp[-1])[:4]}.jpg", quality=72)
print('кадров просмотрено', len(fs))
