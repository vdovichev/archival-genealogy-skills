#!/usr/bin/env python3
"""Обзорная сетка целых страниц: быстро понять структуру дела и найти листы со списками.
   overview_pages.py <папка> <выход> [колонок] [строк] [ширина ячейки]"""
import sys, glob, os
from PIL import Image, ImageDraw
d, out = sys.argv[1], sys.argv[2]
cols = int(sys.argv[3]) if len(sys.argv) > 3 else 4
rows = int(sys.argv[4]) if len(sys.argv) > 4 else 3
CW  = int(sys.argv[5]) if len(sys.argv) > 5 else 620
os.makedirs(out, exist_ok=True)
fs = sorted(glob.glob(os.path.join(d, '0*.jpg')))
per = cols*rows
def thumb(p):
    im = Image.open(p); w,h = im.size
    r = CW/w
    return im.resize((CW, max(1,int(h*r))))
for k in range(0, len(fs), per):
    grp = fs[k:k+per]; cells = [thumb(p) for p in grp]
    CH = max(c.size[1] for c in cells) + 22
    canv = Image.new('RGB', (CW*cols + 6*(cols-1), CH*rows), 'white')
    dr = ImageDraw.Draw(canv)
    for i,(p,c) in enumerate(zip(grp,cells)):
        x = (i%cols)*(CW+6); y = (i//cols)*CH
        dr.text((x+4,y+4), os.path.basename(p)[:4], fill='red')
        canv.paste(c, (x, y+18))
    canv.save(f"{out}/o{os.path.basename(grp[0])[:4]}-{os.path.basename(grp[-1])[:4]}.jpg", quality=68)
print('страниц', len(fs))
