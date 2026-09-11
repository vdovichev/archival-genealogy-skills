#!/usr/bin/env python3
"""Дайджест актовых книг ЗАГС сеткой 3x4: шапка (сельсовет) + фамилия ребёнка."""
import sys, glob, os
from PIL import Image, ImageDraw
d, out = sys.argv[1], sys.argv[2]
cols, rows = 3, 4
ys = [float(x) for x in sys.argv[3:7]] if len(sys.argv) > 6 else [0.125,0.205,0.395,0.475]
os.makedirs(out, exist_ok=True)
fs = sorted(glob.glob(os.path.join(d,'0*.jpg')))
if len(sys.argv)>7 and sys.argv[7]=='even':
    fs=[p for p in fs if int(os.path.basename(p)[:4])%2==0]
CW = 880
def digest(p):
    im = Image.open(p); w,h = im.size
    a = im.crop((int(w*0.28), int(h*ys[0]), int(w*0.97), int(h*ys[1])))
    b = im.crop((int(w*0.28), int(h*ys[2]), int(w*0.97), int(h*ys[3])))
    def sc(x):
        r = CW/x.size[0]; return x.resize((CW, int(x.size[1]*r)))
    a, b = sc(a), sc(b)
    c = Image.new('RGB', (CW, a.size[1]+b.size[1]+6), 'white')
    c.paste(a,(0,0)); c.paste(b,(0,a.size[1]+6))
    return c
per = cols*rows
for k in range(0, len(fs), per):
    grp = fs[k:k+per]
    cells = [digest(p) for p in grp]
    CH = max(c.size[1] for c in cells)+26
    canv = Image.new('RGB',(CW*cols+8*(cols-1), CH*rows),'white')
    dr = ImageDraw.Draw(canv)
    for idx,(p,c) in enumerate(zip(grp,cells)):
        x=(idx%cols)*(CW+8); y=(idx//cols)*CH
        dr.text((x+6,y+6), os.path.basename(p)[:4], fill='red')
        canv.paste(c,(x,y+22))
    canv.save(f"{out}/d{os.path.basename(grp[0])[:4]}-{os.path.basename(grp[-1])[:4]}.jpg", quality=72)
print('готово', len(fs))
