#!/usr/bin/env python3
"""registry_audit — перепроверка реестра персон.

Ловит то, что накапливается тихо: строки без шифра, знак [Д] без образа,
отождествление без основания, дубли упоминаний, несходящуюся арифметику возрастов,
ссылки на несуществующие персоны.

Вход: Markdown-таблица или CSV с колонками (регистр и порядок не важны, часть может
отсутствовать): id, persona, имя_в_документе, имя_норм, пол, роль, событие,
дата_в_документе, дата_норм, возраст, место, сословие, отец, мать, супруг,
шифр, образ, знак, основание, журнал.  Описание полей — REGISTRY.md.

    python3 registry_audit.py РОД-PERSONS-REGISTRY.md [--root .] [--strict]

Код возврата 1, если найдены ошибки (не предупреждения) — удобно для проверки перед сдачей.
"""
import argparse, csv, os, re, sys
from collections import defaultdict

ALIASES = {
    "id": "id", "№": "id", "n": "id",
    "persona": "persona", "персона": "persona", "лицо": "persona",
    "имя_в_документе": "name_doc", "имя в документе": "name_doc", "имя": "name_doc",
    "имя_норм": "name_norm", "пол": "sex", "роль": "role", "событие": "event",
    "дата_в_документе": "date_doc", "дата_норм": "date", "дата": "date",
    "возраст": "age", "место": "place", "сословие": "estate",
    "отец": "father", "мать": "mother", "супруг": "spouse",
    "шифр": "cite", "образ": "image", "знак": "mark",
    "основание": "reason", "журнал": "log",
}
YEAR = re.compile(r"(1[5-9]\d\d|20\d\d)")


def norm_header(h):
    return ALIASES.get(h.strip().lower().strip("*` "), h.strip().lower())


def read_rows(path):
    txt = open(path, encoding="utf-8").read()
    if path.lower().endswith(".csv"):
        return list(csv.DictReader(txt.splitlines()))
    rows, header = [], None
    for line in txt.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue
        if header is None:
            header = [norm_header(c) for c in cells]
            continue
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        rows.append(dict(zip(header, cells[: len(header)])))
    if header is None:
        sys.exit("таблица не найдена: ожидается Markdown-таблица или .csv")
    return rows


def year_of(row):
    for k in ("date", "date_doc"):
        m = YEAR.search(row.get(k, "") or "")
        if m:
            return int(m.group(1))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("registry")
    ap.add_argument("--root", default=".", help="от чего отсчитывать пути к образам")
    ap.add_argument("--strict", action="store_true", help="предупреждения тоже как ошибки")
    a = ap.parse_args()

    rows = read_rows(a.registry)
    err, warn = [], []
    seen_ids, dup_key = {}, defaultdict(list)
    personas = defaultdict(list)

    for i, r in enumerate(rows, 1):
        rid = (r.get("id") or "").strip()
        tag = f"строка {i}" + (f" (id {rid})" if rid else "")
        mark = (r.get("mark") or "").strip()

        if rid:
            if rid in seen_ids:
                err.append(f"{tag}: id повторяется (уже в строке {seen_ids[rid]}); "
                           f"id не переиспользуются даже после удаления")
            seen_ids[rid] = i
        else:
            warn.append(f"{tag}: нет id — на строку нельзя сослаться")

        if not (r.get("cite") or "").strip():
            err.append(f"{tag}: нет шифра — строка без шифра недействительна")

        img = (r.get("image") or "").strip()
        if "[Д]" in mark:
            if not img:
                err.append(f"{tag}: знак [Д] требует пути к образу (иначе это [Д•])")
            elif not re.match(r"https?://", img) and \
                    not os.path.exists(os.path.join(a.root, img)):
                err.append(f"{tag}: образ не найден: {img}")

        if (r.get("persona") or "").strip() and not (r.get("reason") or "").strip():
            err.append(f"{tag}: отождествление без основания — считается не сделанным")

        if (r.get("persona") or "").strip():
            personas[r["persona"].strip()].append((i, r))

        if not mark:
            warn.append(f"{tag}: не проставлен знак достоверности")
        elif mark not in ("[Д]", "[Д•]", "[П]", "[G]", "[Р]", "[?]"):
            warn.append(f"{tag}: незнакомый знак {mark!r}")

        key = "|".join((r.get(k) or "").strip().lower()
                       for k in ("name_doc", "place", "event", "date"))
        if key.strip("|"):
            dup_key[key].append(tag)

        age = (r.get("age") or "").strip()
        if age and not re.search(r"\d", age):
            warn.append(f"{tag}: возраст {age!r} без числа")

    for key, tags in dup_key.items():
        if len(tags) > 1:
            warn.append("возможный дубль упоминания: " + " · ".join(tags) +
                        f"  [{key}]")

    # арифметика возрастов внутри персоны
    for pid, items in personas.items():
        pts = []
        for i, r in items:
            y, age = year_of(r), (r.get("age") or "")
            m = re.search(r"\d+", age)
            if y and m:
                pts.append((y, int(m.group()), i))
        for k in range(len(pts)):
            for j in range(k + 1, len(pts)):
                (y1, a1, i1), (y2, a2, i2) = pts[k], pts[j]
                if y1 == y2:
                    continue
                drift = abs((y2 - y1) - (a2 - a1))
                if drift > 3:
                    err.append(f"персона {pid}: возраст не сходится между строками "
                               f"{i1} ({a1} в {y1}) и {i2} ({a2} в {y2}) — расхождение "
                               f"{drift} лет. Либо это разные люди, либо запись прочитана неверно")

    # ссылки на персон, которых нет
    known = set(personas)
    for i, r in enumerate(rows, 1):
        for fld in ("father", "mother", "spouse"):
            v = (r.get(fld) or "").strip()
            if re.fullmatch(r"[A-Za-zА-Яа-я]+-\d+", v) and v not in known:
                err.append(f"строка {i}: поле {fld} ссылается на неизвестную персону {v}")

    print(f"# строк: {len(rows)} · персон: {len(personas)} · "
          f"ошибок: {len(err)} · предупреждений: {len(warn)}")
    for e in err:
        print("ОШИБКА:      " + e)
    for w in warn:
        print("предупрежд.: " + w)
    if not err and not warn:
        print("Расхождений не найдено. ⚠️ Аудит проверяет форму, а не правду: "
              "выборочную сверку фактов росписи с образами делать вручную (REGISTRY.md §5).")
    sys.exit(1 if err or (a.strict and warn) else 0)


if __name__ == "__main__":
    main()
