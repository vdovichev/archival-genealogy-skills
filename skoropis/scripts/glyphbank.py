#!/usr/bin/env python3
"""glyphbank — банк начертаний писца: нарезка, контактный лист, сравнение.

Метод: спорный глиф НЕ читается «на глаз», а ставится В ОДНОМ МАСШТАБЕ рядом
с кандидатами и с бесспорным словом той же страницы. Скрипт готовит именно
такие сопоставления.

Зависимость: Pillow.

Команды
-------
  cut     СТРАНИЦА OUTDIR --boxes "имя:X,Y,W,H; имя2:X,Y,W,H"
          нарезать эталонные глифы из бесспорных слов страницы
  sheet   BANKDIR OUT [--height 260] [--per 12]
          контактный лист банка с подписями — общая карта начертаний
  cmp     OUT СПОРНЫЙ КАНДИДАТ...
          спорный глиф + кандидаты в один ряд, одна высота (главный приём)
  strokes ГЛИФ [--rows 3]
          профиль вертикальных штрихов: 1 = с · 2 = к · 3 = ск · соединены = т
          (считать ДО того, как назвать слово)
"""
import argparse, glob, os, sys

try:
    from PIL import Image, ImageDraw, ImageOps
except ImportError:
    sys.exit("нужен Pillow:  pip install pillow")

Image.MAX_IMAGE_PIXELS = None


def _fit_h(im, h):
    return im.resize((max(1, round(im.width * h / im.height)), h), Image.LANCZOS)


def _prep(im):
    """Синий канал снимает водяной знак: тёплые чернила в нём темнее, серая печать — светлее."""
    if im.mode == "RGB":
        im = im.split()[2]
    return im.convert("L")


def cmd_cut(a):
    page = _prep(Image.open(a.page))
    os.makedirs(a.outdir, exist_ok=True)
    for chunk in a.boxes.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, box = chunk.split(":")
        x, y, w, h = (int(v) for v in box.split(","))
        out = os.path.join(a.outdir, f"{name.strip()}.png")
        page.crop((x, y, x + w, y + h)).save(out)
        print(out)
    print("# ⚠️ эталон брать ТОЛЬКО из бесспорного слова: эталон из спорного слова — круг")


def cmd_sheet(a):
    files = sorted(glob.glob(os.path.join(a.bankdir, "*.png")) +
                   glob.glob(os.path.join(a.bankdir, "*.jpg")))
    if not files:
        sys.exit("банк пуст")
    tiles = [_fit_h(Image.open(f).convert("L"), a.height) for f in files]
    per = a.per
    rows = (len(tiles) + per - 1) // per
    cw = max(t.width for t in tiles) + 12
    out = Image.new("L", (per * cw + 12, rows * (a.height + 26) + 12), 255)
    d = ImageDraw.Draw(out)
    for i, t in enumerate(tiles):
        r, c = divmod(i, per)
        x, y = 12 + c * cw, 12 + r * (a.height + 26)
        out.paste(t, (x, y))
        d.text((x, y + a.height + 4), os.path.basename(files[i])[:18], fill=0)
    out.save(a.dst)
    print(a.dst, f"# {len(files)} начертаний")


def cmd_cmp(a):
    imgs = [("СПОРНЫЙ", a.disputed)] + [("", c) for c in a.candidates]
    tiles = [(lbl, _fit_h(_prep(Image.open(f)), a.height)) for lbl, f in imgs]
    gap = 16
    W = sum(t.width for _, t in tiles) + gap * (len(tiles) + 1)
    out = Image.new("L", (W, a.height + 40), 255)
    d = ImageDraw.Draw(out)
    x = gap
    for lbl, t in tiles:
        out.paste(t, (x, 28))
        if lbl:
            d.text((x, 6), lbl, fill=0)
        d.line([(x - gap // 2, 0), (x - gap // 2, out.height)], fill=160)
        x += t.width + gap
    out.save(a.dst)
    print(a.dst)
    print("# вердикт — по совпадению ФОРМЫ, а не по «похоже». Версия, требующая "
          "переставить глифы местами, мертва")


def cmd_strokes(a):
    im = ImageOps.autocontrast(_prep(Image.open(a.glyph)))
    w, h = im.size
    px = im.load()
    band = [sum(1 for y in range(int(h * 0.25), int(h * 0.85)) if px[x, y] < 128)
            for x in range(w)]
    thr = max(band) * 0.45 if band else 0
    runs, inside = 0, False
    for v in band:
        if v >= thr and not inside:
            runs, inside = runs + 1, True
        elif v < thr:
            inside = False
    print(f"вертикальных штрихов в теле строки: {runs}")
    print({1: "1 → «с»", 2: "2 → «к» (НЕ «ш»/«и»)", 3: "3 → «ск» (НЕ «ш»)"}
          .get(runs, "больше трёх — вероятно лигатура или несколько букв"))
    print("# соединены поверху → «т». Счёт записать ДО того, как называть слово")


def main():
    ap = argparse.ArgumentParser(prog="glyphbank", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("cut"); p.add_argument("page"); p.add_argument("outdir")
    p.add_argument("--boxes", required=True); p.set_defaults(f=cmd_cut)
    p = sub.add_parser("sheet"); p.add_argument("bankdir"); p.add_argument("dst")
    p.add_argument("--height", type=int, default=260); p.add_argument("--per", type=int, default=12)
    p.set_defaults(f=cmd_sheet)
    p = sub.add_parser("cmp"); p.add_argument("dst"); p.add_argument("disputed")
    p.add_argument("candidates", nargs="+"); p.add_argument("--height", type=int, default=420)
    p.set_defaults(f=cmd_cmp)
    p = sub.add_parser("strokes"); p.add_argument("glyph"); p.set_defaults(f=cmd_strokes)
    a = ap.parse_args(); a.f(a)


if __name__ == "__main__":
    main()
