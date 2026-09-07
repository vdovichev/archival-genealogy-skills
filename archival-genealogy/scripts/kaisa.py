#!/usr/bin/env python3
"""kaisa — личный кабинет движка «КАИСА» (Альт-Софт): каталог, объём оцифровки, образы.

Зачем отдельно от archnet.py: у КАИСА, кроме публичного `/search`, на части установок
есть закрытый `private_api` кабинета. Он отвечает JSON и говорит, **сколько кадров дела
реально снято, до всякой попытки скачивания**. Это снимает самую дорогую ошибку
планирования: в карточке каталога «кадры» означают листы единицы хранения, а не образы
(`PITFALLS.md` §7).

ТОЛЬКО ЧТЕНИЕ. Ничего не создаёт, не изменяет и не удаляет в чужой системе. Заказ копий,
подача требований и оплата — действия человека, не скрипта.

Токен: --token, иначе KAISA_TOKEN, иначе файл --token-file (по умолчанию ~/.kaisa_token).
Держать в режиме 600, в код не вписывать. Живёт около суток, берётся из запроса браузера.

Команды
-------
  alive   HOST                      жив ли доступ (302 -> лимит выбран, 401 -> токен протух)
  find    HOST "Ф. N Оп. N Д. N"    поиск дела по шифру -> objectId, листы, заголовок
  digi    HOST OBJECTID [...]       сколько кадров РЕАЛЬНО оцифровано (можно списком)
  probe   HOST OBJECTID --step 100  карта незнакомого дела: каждый N-й кадр
  pull    HOST OBJECTID OUT --from 1 --to 50 [--size 3]
  tokens  HOST OBJECTID OUT.json    массив адресов кадров (кэш для pull)

Примеры
-------
  python3 kaisa.py alive arch.example.ru
  python3 kaisa.py find  arch.example.ru "Ф. 301 Оп. 5 Д. 827"
  python3 kaisa.py digi  arch.example.ru 745544132 779949216 728832335
  python3 kaisa.py probe arch.example.ru 779949216 --step 100 --out map/
  python3 kaisa.py pull  arch.example.ru 745544132 out/ --from 656 --to 676 --size 4

Через страновой узел: --socks 127.0.0.1:11080 --ca /path/ca-bundle.pem
Номера атрибутов/групп различаются по установке: --attr-image, --group-image, --attr-code.
"""
import argparse, base64, json, os, re, subprocess, sys, urllib.parse

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
STUB_MAX = 4000          # HTML-заглушка при исчерпанном лимите ~1,3 КБ; JPEG кадра — сотни КБ
CARD_ATTRS = [6725842, 4888, 5193365, 4894, 4952, 4896, 4920]


def get_token(a):
    if a.token:
        return a.token.strip()
    if os.environ.get("KAISA_TOKEN"):
        return os.environ["KAISA_TOKEN"].strip()
    p = os.path.expanduser(a.token_file)
    if os.path.exists(p):
        return open(p).read().strip()
    sys.exit("нет токена: --token, KAISA_TOKEN или %s" % p)


def token_expiry(tok):
    """Срок жизни JWT без обращения к серверу: (истёк?, строка со сроком).

    Токен кабинета живёт около суток. Протухший даёт пустые ответы API — и это
    выглядит как «нет прав» или «дело не найдено», хотя лечится обновлением токена.
    """
    import time
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        s = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp))
        return exp < time.time(), s
    except Exception:
        return False, None


def hint_token(a):
    """Подсказка при пустом ответе: чаще всего виноват протухший токен."""
    dead, when = token_expiry(get_token(a))
    if dead:
        return "  ⚠️ ТОКЕН ПРОТУХ (истёк %s) — обновить из запроса браузера в кабинет" % when
    if when:
        return "  (токен годен до %s — значит дело в правах или в номере атрибута)" % when
    return "  (токен не разобрать — проверить, что в файле именно Bearer-строка)"


def run(a, cmd, **kw):
    env = dict(os.environ)
    if a.ca:
        env["SSL_CERT_FILE"] = a.ca
    return subprocess.run(cmd, capture_output=True, env=env, **kw)


def curl_base(a):
    c = ["curl", "-sS", "-m", str(a.timeout), "-A", UA]
    if a.socks:
        c += ["--socks5-hostname", a.socks]
    return c


def api(a, path, body):
    t = get_token(a)
    cmd = curl_base(a) + [
        "-H", "Authorization: Bearer " + t,
        "-H", "Content-Type: application/json",
        "-H", "Accept: application/json, text/plain, */*",
        "-H", "Origin: https://%s" % a.host,
        "-H", "Referer: https://%s/private/documents" % a.host,
        "--data-raw", json.dumps(body, ensure_ascii=False),
        "https://%s/private_api/%s" % (a.host, path)]
    return run(a, cmd).stdout


def viewer_url(a, oid):
    return ("https://%s/srv/private/imageViewer/show?objectId=%s&attributeId=%d"
            "&serial=1&group=%d&ext=.jpg" % (a.host, oid, a.attr_image, a.group_image))


# ---------------------------------------------------------------- команды

def cmd_alive(a):
    t = get_token(a)
    cmd = curl_base(a) + ["-o", "/dev/null", "-w", "%{http_code}",
                          "-H", "Authorization: Bearer " + t,
                          "-b", "auth._token.local=Bearer%%20%s" % t,
                          viewer_url(a, a.objectid or "1")]
    code = run(a, cmd).stdout.decode().strip()
    print("HTTP %s — %s" % (code, {
        "200": "доступ есть",
        "302": "ЛИМИТ ВЫБРАН или абонемент не оплачен (редирект на /subscription)",
        "401": "токен протух — обновить из браузера",
        "403": "нет прав на это дело",
    }.get(code, "неожиданный код")))
    if code == "302":
        print("  ⚠️ В этом состоянии проверка «оцифровано ли дело» через просмотрщик ЛЖЁТ:")
        print("     302 приходит на ЛЮБОЕ дело. Объём смотреть командой digi — она работает.")


def cmd_find(a):
    body = {"objectTypeId": 4220, "languageId": 1, "pageSize": a.limit, "pageNumber": 1,
            "attributes": CARD_ATTRS,
            "searchParams": [{"attributeId": a.attr_code, "searchConditionId": 12,
                              "value": a.query}]}
    out = api(a, "get-object-list", body)
    try:
        rows = json.loads(out)["value"]["list"]
    except Exception:
        print("не разобрать ответ: %s" % out[:200].decode("utf-8", "replace"))
        sys.exit(hint_token(a))
    if not rows:
        print("не найдено")
        return
    for it in rows:
        v = it["attributeValues"]
        g = lambda k, f="string": (v.get(str(k)) or {}).get(f)
        print("%-24s | листов: %-6s | %s" % (g(a.attr_code) or it["name"],
                                             g(4952, "decimal"), (g(4894) or "")[:64]))
        print("    objectId=%s" % it["id"])


def digitized(a, oid):
    out = api(a, "get-object", {"objectId": int(oid), "languageId": 1})
    try:
        v = json.loads(out)["value"]
        n = int(v.get("groupRecordCounts", {}).get(str(a.group_image), 0))
        return v.get("name"), n
    except Exception:
        return None, None


def cmd_digi(a):
    for oid in a.objectids:
        name, n = digitized(a, oid)
        if n is None:
            print("%s: не разобрать ответ" % oid)
            print(hint_token(a))
            return
        note = ""
        if n == 0:
            note = "  <- образов НЕТ: только заказ копий"
        elif n < 60:
            note = "  <- МАЛО: похоже на чужие заказные копии отдельных листов, не дело"
        print("%-26s кадров: %-6d%s" % (name or oid, n, note))


def image_tokens(a, oid):
    t = get_token(a)
    cmd = curl_base(a) + ["-H", "Authorization: Bearer " + t,
                          "-b", "auth._token.local=Bearer%%20%s" % t,
                          "-H", "Referer: https://%s/private/documents" % a.host,
                          viewer_url(a, oid)]
    html = run(a, cmd).stdout.decode("utf-8", "replace")
    m = re.search(r"img\.src\s*=\s*hostPath\s*\+\s*'/image\?url='\s*\+\s*([A-Za-z_]+)\[", html)
    if not m:
        return None
    blk = re.search(r"var\s+" + m.group(1) + r"\s*=\s*\[(.*?)\];", html, re.S)
    return re.findall(r"'([^']*)'", blk.group(1)) if blk else None


def cmd_tokens(a):
    arr = image_tokens(a, a.objectid)
    if not arr:
        print("массив кадров не найден: лимит выбран (см. alive) или дело не оцифровано")
        sys.exit(hint_token(a))
    json.dump(arr, open(a.out, "w"))
    print("кадров: %d -> %s" % (len(arr), a.out))


def frame_url(tok, size):
    """Адрес кадра: строка XOR 0xFF в base64, внутри — параметры с size."""
    s = bytes(c ^ 0xFF for c in base64.b64decode(tok)).decode()
    s = re.sub(r"size=\d+", "size=" + str(size), s)
    return urllib.parse.quote(
        base64.b64encode(bytes(c ^ 0xFF for c in s.encode())).decode(), safe="")


def fetch(a, oid, arr, frames, outdir, size):
    os.makedirs(outdir, exist_ok=True)
    todo = []
    for i in frames:
        if not (1 <= i <= len(arr)):
            continue
        p = os.path.join(outdir, "k%d.jpg" % i)
        if os.path.exists(p) and os.path.getsize(p) > STUB_MAX:
            continue
        todo.append((p, frame_url(arr[i - 1], size)))
    if not todo:
        print("всё нужное уже скачано")
        return 0, 0
    t = get_token(a)
    socks = ("--socks5-hostname " + a.socks) if a.socks else ""
    script = ('f="${1%%%%\t*}"; u="${1#*\t}"; '
              'curl -s -m 180 %s -A "$UA" -H "Authorization: Bearer $T" '
              '-b "auth._token.local=Bearer%%20$T" -H "Referer: $REF" '
              '"https://%s/srv/private/imageViewer/image?url=$u" -o "$f"' % (socks, a.host))
    env = dict(os.environ, T=t, UA=UA, REF=viewer_url(a, oid))
    if a.ca:
        env["SSL_CERT_FILE"] = a.ca
    subprocess.run(["xargs", "-P", str(a.workers), "-I", "{}", "bash", "-c", script, "_", "{}"],
                   input="\n".join("%s\t%s" % x for x in todo).encode(), env=env)
    stubs = [p for p, _ in todo if os.path.exists(p) and os.path.getsize(p) <= STUB_MAX]
    for p in stubs:
        os.remove(p)
    got = sum(1 for p, _ in todo if os.path.exists(p))
    print("скачано %d, отброшено заглушек %d -> %s" % (got, len(stubs), outdir))
    if stubs and got == 0:
        print("  ⚠️ ВСЁ заглушки: лимит просмотров выбран. Проверить: kaisa.py alive")
    return got, len(stubs)


def cmd_pull(a):
    arr = json.load(open(a.tokens)) if a.tokens else image_tokens(a, a.objectid)
    if not arr:
        print("массив кадров не получен (см. alive)")
        sys.exit(hint_token(a))
    to = min(a.to or len(arr), len(arr))
    fetch(a, a.objectid, arr, range(a.frm, to + 1), a.out, a.size)


def cmd_probe(a):
    """Карта незнакомого дела: каждый N-й кадр в обзорном разрешении.

    Дело на 2000+ кадров без внутренней описи иначе не охватить. По шапкам проб
    строится соответствие «кадр -> селение/лист», дальше качается только нужный участок.
    """
    arr = image_tokens(a, a.objectid)
    if not arr:
        print("массив кадров не получен (см. alive)")
        sys.exit(hint_token(a))
    frames = list(range(a.step, len(arr) + 1, a.step))
    print("дело: %d кадров, проб: %d (шаг %d)" % (len(arr), len(frames), a.step))
    fetch(a, a.objectid, arr, frames, a.out, a.size)
    print("\nдальше — прочитать шапки одной мозаикой:")
    print("  python3 archimg.py montage map.png '%s/*.jpg' --tile 3x4 --label" % a.out)
    print("и записать соответствие «кадр -> селение» в журнал рода (WORKFLOW.md §2).")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--socks", help="SOCKS5 узла нужной страны, напр. 127.0.0.1:11080")
    p.add_argument("--ca", help="свой CA-бандл (SSL_CERT_FILE)")
    p.add_argument("--token")
    p.add_argument("--token-file", default="~/.kaisa_token")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--attr-image", type=int, default=5908, help="атрибут файла образа")
    p.add_argument("--group-image", type=int, default=4688, help="группа записей с образами")
    p.add_argument("--attr-code", type=int, default=6725842, help="атрибут шифра дела")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("alive"); s.add_argument("host")
    s.add_argument("--objectid"); s.set_defaults(f=cmd_alive)

    s = sub.add_parser("find"); s.add_argument("host"); s.add_argument("query")
    s.add_argument("--limit", type=int, default=10); s.set_defaults(f=cmd_find)

    s = sub.add_parser("digi"); s.add_argument("host")
    s.add_argument("objectids", nargs="+"); s.set_defaults(f=cmd_digi)

    s = sub.add_parser("tokens"); s.add_argument("host"); s.add_argument("objectid")
    s.add_argument("out"); s.set_defaults(f=cmd_tokens)

    s = sub.add_parser("pull"); s.add_argument("host"); s.add_argument("objectid")
    s.add_argument("out"); s.add_argument("--from", dest="frm", type=int, default=1)
    s.add_argument("--to", type=int); s.add_argument("--size", type=int, default=3)
    s.add_argument("--tokens"); s.set_defaults(f=cmd_pull)

    s = sub.add_parser("probe"); s.add_argument("host"); s.add_argument("objectid")
    s.add_argument("--step", type=int, default=100); s.add_argument("--out", default="probe")
    s.add_argument("--size", type=int, default=3); s.set_defaults(f=cmd_probe)

    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
