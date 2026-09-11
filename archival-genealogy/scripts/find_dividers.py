#!/usr/bin/env python3
"""Ищет листы-разделители в папке кадров: почти пустой центр + светлый фон.
   Чёрную рамку сканера игнорируем — смотрим только середину кадра."""
import sys, glob, os
from PIL import Image
import numpy as np
d = sys.argv[1]
for p in sorted(glob.glob(os.path.join(d,'0*.jpg'))):
    a = np.asarray(Image.open(p).convert('L').resize((300,380)), dtype=np.uint8)
    c = a[40:340, 30:270]
    dark = (c < 110).mean()
    if dark < 0.012 and c.mean() > 175:
        print(os.path.basename(p), 'тёмных', round(dark*100,2), 'яркость', int(c.mean()))
