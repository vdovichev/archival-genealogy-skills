#!/usr/bin/env python3
"""archimg — обработка архивных образов без внешних утилит.

Заменяет ImageMagick (convert/montage/identify) и poppler (pdftoppm/pdfinfo)
на чистый Python, чтобы приёмы работали в песочнице без установленных бинарников.

Зависимости: Pillow (обязательно), PyMuPDF (только для PDF; при отсутствии
команда pdf сообщит об этом и предложит подать уже готовые картинки).

Команды
-------
  info      FILE...                        размеры, ориентация, диагноз «разворот/страница»
  pdf       IN.pdf OUTDIR [--pages 1-20] [--dpi 200]
  prep      IN OUT [--crop X,Y,W,H] [--channel B] [--width 2000]
            [--levels 9,73] [--sharpen] [--invert] [--gray]
  column    PAGE OUT [--frac 0.24] [--halves 2] [--height 1500]
            приём «колонка деревни»: левый край каждой полустраницы
  montage   OUT IN...  [--tile 10x2] [--height 420] [--label] [--bg '#888888']
  sheet     INDIR OUT  [--per 6] [--width 800]   контактный лист разворотов
  split     IN OUTPREFIX [--parts 2]             разворот → отдельные страницы
  bands     IN OUTPREFIX --line Y,H [--x 0] [--w 0]
            двухслойное чтение строки: верхний пояс (выносные) + тело

Все команды печатают путь(и) результата — их и открывать на просмотр.
"""
import argparse, os, sys, glob

try:
    from PIL import Image, ImageFilter, ImageOps, ImageDraw
except ImportError:
    sys.exit("нужен Pillow:  pip install pillow")

Image.MAX_IMAGE_PIXELS = None          # архивные развороты бывают >178 Мпикс


# ---------------------------------------------------------------- helpers
def _levels(im, lo_pct, hi_pct):
    """Растяжка контраста: точки ниже lo → чёрное, выше hi → белое."""
    lo, hi = lo_pct * 255 / 100.0, hi_pct * 255 / 100.0
    if hi <= lo:
        return im
    scale = 255.0 / (hi - lo)
    return im.point(lambda v: 0 if v <= lo else (255 if v >= hi else int((v - lo) * scale)))


def _open(path):
    im = Image.open(path)
    im.load()
    return im


def _fit_h(im, h):
    if not h or im.height == h:
        return im
    return im.resize((max(1, round(im.width * h / im.height)), h), Image.LANCZOS)


def _save(im, path):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if path.lower().endswith((".jpg", ".jpeg")):
        im.convert("RGB").save(path, quality=95, subsampling=0)
    else:
        im.save(path)
    print(path)


def _parse_pages(spec, n):
    if not spec:
        return list(range(n))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a) - 1, min(int(b), n)))
        else:
            out.append(int(part) - 1)
    return [p for p in out if 0 <= p < n]


# ---------------------------------------------------------------- commands
def cmd_info(a):
    for pat in a.files:
        for f in sorted(glob.glob(pat)) or [pat]:
            try:
                im = Image.open(f)
            except Exception as e:
                print(f"{f}: НЕ ОТКРЫЛСЯ — {e}")
                continue
            w, h = im.size
            ratio = w / h
            kind = ("разворот (ландшафт) — вероятно ДВЕ страницы"
                    if ratio > 1.15 else
                    "страница (портрет)" if ratio < 0.9 else "квадрат — проверить глазами")
            print(f"{f}: {w}x{h}  ratio {ratio:.2f}  {im.mode}  → {kind}")
    print("\n⚠️ Смотреть файл из СЕРЕДИНЫ дела: первый кадр обычно обложка с другой геометрией.")


def cmd_pdf(a):
    try:
        import fitz
    except ImportError:
        sys.exit("для PDF нужен PyMuPDF (pip install pymupdf). "
                 "Либо подайте уже готовые картинки страниц.")
    doc = fitz.open(a.pdf)
    pages = _parse_pages(a.pages, doc.page_count)
    os.makedirs(a.outdir, exist_ok=True)
    zoom = a.dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    for i in pages:
        pix = doc[i].get_pixmap(matrix=mat)
        out = os.path.join(a.outdir, f"p{i+1:04d}.png")
        pix.save(out)
        print(out)
    print(f"# страниц в файле: {doc.page_count}, отрисовано: {len(pages)}, dpi={a.dpi}")


def cmd_prep(a):
    im = _open(a.src)
    if a.crop:
        x, y, w, h = (int(v) for v in a.crop.split(","))
        im = im.crop((x, y, x + w, y + h))
    if a.channel:
        # тёплые чернила темнее в синем канале, серый водяной знак — светлее:
        # разделение каналов снимает печать поверх текста
        idx = {"R": 0, "G": 1, "B": 2}[a.channel.upper()]
        im = im.convert("RGB").split()[idx]
    elif a.gray:
        im = im.convert("L")
    if a.invert:
        im = ImageOps.invert(im.convert("L"))
    if a.width and im.width != a.width:
        im = im.resize((a.width, max(1, round(im.height * a.width / im.width))), Image.LANCZOS)
    if a.levels:
        lo, hi = (float(v) for v in a.levels.split(","))
        im = _levels(im.convert("L"), lo, hi)
    if a.sharpen:
        im = im.filter(ImageFilter.UnsharpMask(radius=1.4, percent=140, threshold=2))
    _save(im, a.dst)


def cmd_column(a):
    """Левый край каждой полустраницы — там, где в метриках стоит НАЗВАНИЕ ДЕРЕВНИ.

    Даёт просмотр десятков разворотов за один взгляд: деревня пишется устойчивее
    фамилии, и её колонка узнаётся даже без чтения текста."""
    im = _open(a.src).convert("L")
    W, H = im.size
    part = W // a.halves
    cw = max(1, int(part * a.frac))
    cols = []
    for k in range(a.halves):
        c = im.crop((k * part, 0, k * part + cw, H))
        cols.append(_fit_h(_levels(c, 8, 75), a.height))
    gap, bg = 8, 136
    total = sum(c.width for c in cols) + gap * (len(cols) + 1)
    out = Image.new("L", (total, a.height + 2 * gap), bg)
    x = gap
    for c in cols:
        out.paste(c, (x, gap))
        x += c.width + gap
    _save(out, a.dst)


def cmd_montage(a):
    files = []
    for pat in a.files:
        files += sorted(glob.glob(pat)) or [pat]
    if not files:
        sys.exit("нет входных файлов")
    tw, th = (int(v) for v in a.tile.lower().split("x"))
    tiles = [_fit_h(_open(f).convert("RGB"), a.height) for f in files]
    if a.label:
        for t, f in zip(tiles, files):
            d = ImageDraw.Draw(t)
            name = os.path.basename(f)
            d.rectangle([0, t.height - 16, min(t.width, 9 * len(name)), t.height], fill="white")
            d.text((2, t.height - 15), name, fill="black")
    rows = th or ((len(tiles) + tw - 1) // tw)
    gap = 8
    colw = max(t.width for t in tiles) + gap
    out = Image.new("RGB", (tw * colw + gap, rows * (a.height + gap) + gap), a.bg)
    for i, t in enumerate(tiles[: tw * rows]):
        r, c = divmod(i, tw)
        out.paste(t, (gap + c * colw, gap + r * (a.height + gap)))
    if len(tiles) > tw * rows:
        print(f"# ⚠️ показаны первые {tw*rows} из {len(tiles)} — увеличьте --tile", file=sys.stderr)
    _save(out, a.dst)


def cmd_sheet(a):
    files = sorted(glob.glob(os.path.join(a.indir, "*")))
    files = [f for f in files if f.lower().endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff"))]
    if not files:
        sys.exit("в папке нет образов")
    tiles = []
    for f in files:
        im = _open(f).convert("RGB")
        tiles.append(im.resize((a.width, max(1, round(im.height * a.width / im.width))),
                               Image.LANCZOS))
    h = max(t.height for t in tiles)
    per = a.per
    rows = (len(tiles) + per - 1) // per
    gap = 6
    out = Image.new("RGB", (per * (a.width + gap) + gap, rows * (h + gap) + gap), "#777777")
    d = ImageDraw.Draw(out)
    for i, t in enumerate(tiles):
        r, c = divmod(i, per)
        x, y = gap + c * (a.width + gap), gap + r * (h + gap)
        out.paste(t, (x, y))
        d.text((x + 3, y + 3), os.path.basename(files[i])[:22], fill="yellow")
    _save(out, a.dst)
    print(f"# {len(files)} образов; ищите визуально уникальное: НАРИСОВАННЫЕ СЕТКИ "
          f"(итоговые таблицы) — они называют селение явно")


def cmd_split(a):
    im = _open(a.src)
    W, H = im.size
    n = a.parts
    for k in range(n):
        _save(im.crop((k * W // n, 0, (k + 1) * W // n, H)), f"{a.prefix}_{k+1}.png")


def cmd_bands(a):
    """Двухслойное чтение: служебные слова и окончания выносятся НАД строкой.
    Верхний пояс читается отдельно как самостоятельный текст — иначе он
    воспринимается как фон и даёт пропуски дат («де» = день, «зу» = указу)."""
    im = _open(a.src).convert("L")
    y, h = (int(v) for v in a.line.split(","))
    x = a.x
    w = a.w or (im.width - x)
    up = im.crop((x, y, x + w, y + int(h * 0.45)))
    base = im.crop((x, y + int(h * 0.35), x + w, y + h))
    for tag, part in (("up", up), ("base", base)):
        p = part.resize((2200, max(1, round(part.height * 2200 / part.width))), Image.LANCZOS)
        p = _levels(p, 10, 72).filter(ImageFilter.UnsharpMask(radius=1.4, percent=140, threshold=2))
        _save(p, f"{a.prefix}_{tag}.png")
    print("# читать СНАЧАЛА _up (что за буквы/слоги стоят над строкой), потом _base")


# ---------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(prog="archimg", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("info"); p.add_argument("files", nargs="+"); p.set_defaults(f=cmd_info)

    p = sub.add_parser("pdf")
    p.add_argument("pdf"); p.add_argument("outdir")
    p.add_argument("--pages"); p.add_argument("--dpi", type=int, default=200)
    p.set_defaults(f=cmd_pdf)

    p = sub.add_parser("prep")
    p.add_argument("src"); p.add_argument("dst")
    p.add_argument("--crop"); p.add_argument("--channel", choices=list("RGBrgb"))
    p.add_argument("--width", type=int, default=0); p.add_argument("--levels")
    p.add_argument("--sharpen", action="store_true"); p.add_argument("--invert", action="store_true")
    p.add_argument("--gray", action="store_true")
    p.set_defaults(f=cmd_prep)

    p = sub.add_parser("column")
    p.add_argument("src"); p.add_argument("dst")
    p.add_argument("--frac", type=float, default=0.24); p.add_argument("--halves", type=int, default=2)
    p.add_argument("--height", type=int, default=1500)
    p.set_defaults(f=cmd_column)

    p = sub.add_parser("montage")
    p.add_argument("dst"); p.add_argument("files", nargs="+")
    p.add_argument("--tile", default="10x2"); p.add_argument("--height", type=int, default=420)
    p.add_argument("--label", action="store_true"); p.add_argument("--bg", default="#888888")
    p.set_defaults(f=cmd_montage)

    p = sub.add_parser("sheet")
    p.add_argument("indir"); p.add_argument("dst")
    p.add_argument("--per", type=int, default=6); p.add_argument("--width", type=int, default=800)
    p.set_defaults(f=cmd_sheet)

    p = sub.add_parser("split")
    p.add_argument("src"); p.add_argument("prefix"); p.add_argument("--parts", type=int, default=2)
    p.set_defaults(f=cmd_split)

    p = sub.add_parser("bands")
    p.add_argument("src"); p.add_argument("prefix"); p.add_argument("--line", required=True)
    p.add_argument("--x", type=int, default=0); p.add_argument("--w", type=int, default=0)
    p.set_defaults(f=cmd_bands)

    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
