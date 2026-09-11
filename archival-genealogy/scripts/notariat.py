#!/usr/bin/env python3
"""Реестр наследственных дел ФНП: проверка гипотез по ПОЛНОМУ ФИО.

API: POST https://notariat.ru/api/probate-cases, JSON {name, birth_date, death_date}.
Совпадение только по полному ФИО целиком — перебрать фамилию нельзя, можно только
проверять готовые сочетания. Отсюда режим `--pattern`: подставляет имена в шаблон.

  python3 notariat.py --fio "Фамилия Имя Отчество"
  python3 notariat.py --pattern "Фамилия {} Отчество" --names male.txt --json out.json
"""
import argparse, json, subprocess, sys, time

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
URL = "https://notariat.ru/api/probate-cases"


SOCKS = ""


def ask(fio, birth="NULL", death="NULL"):
    body = json.dumps({"name": fio, "birth_date": birth, "death_date": death},
                      ensure_ascii=False)
    cmd = ["curl", "-sS", "-m", "45", "-A", UA, "-H", "Content-Type: application/json",
           "-H", "Referer: https://notariat.ru/ru-ru/help/probate-cases/",
           "-X", "POST", URL, "--data-raw", body]
    if SOCKS:
        cmd[1:1] = ["--socks5-hostname", SOCKS]
    out = subprocess.run(cmd, capture_output=True).stdout.decode("utf-8", "replace")
    try:
        return json.loads(out)
    except Exception:
        return {"count": None, "records": [], "_raw": out[:200]}


def short(r):
    return (f"{r['Fio']} | р. {r.get('BirthDate')} | ум. {r.get('DeathDate')} | "
            f"{r.get('Address')} | {r.get('DistrictName')} | дело {r.get('CaseNumber')} "
            f"| нотариус {r.get('NotaryName')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fio"); ap.add_argument("--pattern"); ap.add_argument("--names")
    ap.add_argument("--birth", default="NULL"); ap.add_argument("--death", default="NULL")
    ap.add_argument("--pause", type=float, default=0.4); ap.add_argument("--json", default="")
    ap.add_argument("--socks", default="", help="хост:порт SOCKS5 — свой IP быстро упирается в лимит")
    a = ap.parse_args()
    globals()['SOCKS'] = a.socks

    hits, checked, errors = [], 0, 0
    if a.fio:
        cand = [a.fio]
    else:
        names = [l.strip() for l in open(a.names) if l.strip()]
        cand = [a.pattern.format(n) for n in names]
    for fio in cand:
        d = ask(fio, a.birth, a.death)
        checked += 1
        if d["count"] is None:
            errors += 1
            sys.stderr.write("!! %s -> %s\n" % (fio, d.get("_raw")))
        elif d["count"]:
            for r in d["records"]:
                hits.append(r)
                print("🎯", short(r))
        if checked % 25 == 0:
            sys.stderr.write("  ...%d/%d, попаданий %d, ошибок %d\n"
                             % (checked, len(cand), len(hits), errors))
        time.sleep(a.pause)
    sys.stderr.write("проверено %d, попаданий %d, ошибок %d\n" % (checked, len(hits), errors))
    if a.json:
        json.dump(hits, open(a.json, "w"), ensure_ascii=False, indent=1)
