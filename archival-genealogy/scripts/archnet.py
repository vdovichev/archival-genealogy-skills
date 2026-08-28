#!/usr/bin/env python3
"""archnet — коннекторы к архивным поисковым системам. ТОЛЬКО ЧТЕНИЕ.

Стандартная библиотека, без зависимостей. Все запросы — GET/POST поиска;
ничего не создаёт и не изменяет на стороне источника.

⚠️ Источник закрыт по географии? Поднимите SOCKS одной командой, ничего не устанавливая
на удалённой машине:  `ssh -D 11080 -N you@узел-в-нужной-стране`  — и добавьте к любой
команде `--socks 127.0.0.1:11080`.

⚠️ Требует выхода в сеть. В песочнице без интернета (веб-чат) команды вернут
ошибку соединения — это не поломка скрипта. Там пользуйтесь скиллом как
справочником, а запросы выполняйте в среде с сетью.

Команды
-------
  af      BASE_URL ЗАПРОС [--digital] [--from Y] [--to Y]
          движок «Архивный фонд»: ищет ЗАГОЛОВКИ фондов/описей/дел, не людей.
          Запрос формулировать топонимом + видом документа.
  kaisa   BASE_URL ЗАПРОС
          движок «КАИСА» (Альт-Софт). Выдача в Nuxt-пейлоаде, 10 на страницу.
  epav    ЗАПРОС [--fulltext] [--provider VUB|ARCH] [--page N] [--size N]
          национальный портал оцифровки Литвы (epaveldas.lt).
  epav-id ИДЕНТИФИКАТОР                карточка документа
  oai     BASE ГЛАГОЛ [--set S] [--prefix oai_edm] [--id ID] [--from Y-M-D]
          OAI-PMH: GET-вход в портал оцифровки, работает и там, где поиск только POST.
          BASE — например https://<портал>/oai/OAIHandler
  post    URL --json ТЕЛО [--referer R]
          POST-запрос к внутреннему шлюзу поиска. Нужен, когда поиск портала сделан
          не ссылкой, а запросом с телом (Elasticsearch и подобные) — см. RECIPES.md,
          «Поиск, спрятанный за POST».
  cdx     ДОМЕН [--filter .pdf]        что осталось от мёртвого сайта в веб-архиве
  probe   URL...                       живой ли адрес: код, редиректы, тип, сертификат

Примеры
-------
  archnet.py af https://af.example-archive.ru "Иванцевская волость метрические книги"
  archnet.py epav "Rajuny" --fulltext
  archnet.py cdx someschool.example.lt --filter .pdf
"""
import argparse, http.client, json, re, socket, ssl, struct, sys, urllib.parse, urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
TIMEOUT = 60


# ------------------------------------------------------- SOCKS5 без зависимостей
class _Socks5Connection(http.client.HTTPSConnection):
    """HTTPS через SOCKS5-прокси. Нужен, когда источник закрыт по географии:
    `ssh -D <порт> -N you@узел-в-стране` поднимает такой прокси без установки чего-либо
    на самом узле. Разрешение имени отдаётся прокси (аналог socks5h)."""
    proxy = None                       # (host, port) — ставится в set_socks()

    def connect(self):
        self.sock = self._context.wrap_socket(_socks_connect(self), server_hostname=self.host)


def _socks_connect(conn):
    """Рукопожатие SOCKS5 и возврат готового сокета (без TLS)."""
    ph, pp = _Socks5Connection.proxy
    s = socket.create_connection((ph, pp), conn.timeout)
    s.sendall(b"\x05\x01\x00")
    if s.recv(2) != b"\x05\x00":
        raise OSError(f"SOCKS5 {ph}:{pp} не принял соединение без пароля")
    host = conn.host.encode("idna")
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + struct.pack(">H", conn.port))
    rep = s.recv(4)
    if len(rep) < 2 or rep[1] != 0:
        code = rep[1] if len(rep) > 1 else -1
        raise OSError(f"SOCKS5 отказал (код {code}) при подключении к {conn.host}:{conn.port}")
    atyp = rep[3]
    s.recv(4 if atyp == 1 else (16 if atyp == 4 else s.recv(1)[0]))
    s.recv(2)
    return s


class _Socks5PlainConnection(http.client.HTTPConnection):
    """Тот же прокси для обычного http:// — иначе часть запросов молча пошла бы напрямую
    и «работает» означало бы «работает мимо туннеля»."""
    def connect(self):
        self.sock = _socks_connect(self)


class _Socks5Handler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(self._make, req)

    def _make(self, host, **kw):
        kw.pop("context", None)
        return _Socks5Connection(host, context=self._context, **kw)


class _Socks5PlainHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_Socks5PlainConnection, req)


def set_socks(spec, insecure=False):
    """spec — 'host:port'. Ставит SOCKS-обработчик глобально для urllib."""
    h, _, p = spec.rpartition(":")
    _Socks5Connection.proxy = (h or "127.0.0.1", int(p))
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    hnd = _Socks5Handler()
    hnd._context = ctx
    _Socks5Connection._context = ctx
    urllib.request.install_opener(urllib.request.build_opener(hnd, _Socks5PlainHandler()))
    print(f"# трафик идёт через SOCKS5 {_Socks5Connection.proxy[0]}:"
          f"{_Socks5Connection.proxy[1]}", file=sys.stderr)


def get(url, data=None, headers=None, insecure=False, timeout=TIMEOUT):
    # часть сайтов отдаёт 403 на «голый» запрос: нужен полный набор заголовков браузера
    h = {"User-Agent": UA,
         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
         "Accept-Language": "ru,en;q=0.8", "Connection": "close"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = data.encode() if isinstance(data, str) else data
    req = urllib.request.Request(url, data=body, headers=h)
    ctx = None
    if insecure:
        # некоторые архивы отдают цепочку, которой нет в системном хранилище —
        # это НЕ «сайт недоступен», см. PITFALLS.md
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.status, r.headers, r.read()


def _text(html, limit=None):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] if limit else t


# ------------------------------------------------------------------ AF
def cmd_af(a):
    q = {"search": a.query, "annotation": "Y", "morph": "Y",
         "from": a.dt_from or "", "to": a.dt_to or ""}
    if a.digital:
        q["digitalCopy"] = "on"
    url = a.base.rstrip("/") + "/archive/search?" + urllib.parse.urlencode(q)
    st, hd, raw = get(url, insecure=a.insecure)
    html = raw.decode("utf-8", "replace")
    ids = re.findall(r'/archive1/(unit|inventory|funds)/(\d+)', html)
    titles = re.findall(r'<a[^>]+/archive1/\w+/\d+[^>]*>(.*?)</a>', html, re.S)
    print(f"# HTTP {st}  {url}")
    seen = set()
    for (kind, i), t in zip(ids, titles):
        key = (kind, i)
        if key in seen:
            continue
        seen.add(key)
        print(f"{kind:9} {i:>8}  {_text(t, 160)}")
    if not seen:
        print("# ничего не разобрано. Проверить: (1) выдача не пуста глазами, "
              "(2) флаг --digital обнуляет выдачу в части установок, "
              "(3) не блокирует ли доступ страна IP.")


# ---------------------------------------------------------------- KAISA
def cmd_kaisa(a):
    url = (a.base.rstrip("/") + "/search?" +
           urllib.parse.urlencode({"type": "simple", "p.0.v": a.query,
                                   "p.0.c": "1582", "p.0.a": "15868"}))
    st, hd, raw = get(url, insecure=a.insecure)
    html = raw.decode("utf-8", "replace")
    print(f"# HTTP {st}  {url}")
    m = re.search(r'window\.__NUXT__\s*=\s*(.+?);?\s*</script>', html, re.S)
    if not m:
        print(_text(html, 3000))
        print("\n# пейлоад Nuxt не найден — движок мог обновиться, разбирать HTML")
        return
    blob = m.group(1)
    for t in sorted(set(re.findall(r'"(?:title|name|header)"\s*:\s*"([^"]{8,300})"', blob))):
        print("-", t.encode().decode("unicode_escape"))
    print("\n# ⚠️ пагинация: 10 записей на страницу, счётчики выше 10 обрезаются")


# -------------------------------------------------------------- epaveldas
EPAV = "https://www.epaveldas.lt/vepis-api/internal"


def cmd_epav(a):
    body = {"term": a.query, "page": a.page, "pageSize": a.size,
            "includeFullText": bool(a.fulltext), "filters": {}}
    if a.provider:
        body["filters"] = {"providerFacets_str": [a.provider]}
    st, hd, raw = get(EPAV + "/find", data=json.dumps(body),
                      headers={"Content-Type": "application/json"})
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit("ответ не JSON — вероятно вернулась оболочка SPA, маршрут изменился")
    items = d.get("items") or d.get("content") or d.get("results") or []
    print(f"# HTTP {st}  найдено: {d.get('count', '?')}  страниц: {d.get('pagesCount', '?')}")
    for it in items:
        i = it.get("id") or it.get("sourceIdentifier") or "?"
        ttl = it.get("title") or it.get("name") or ""
        prov = (it.get("provider") or "")[:28]
        hl = it.get("highlightsCount") or 0
        print(f"{i:26} {prov:28} {_text(str(ttl), 130)}" + (f"   [OCR-совпадений: {hl}]" if hl else ""))
    if items and a.fulltext:
        print("# у записей с [OCR-совпадений] фамилия найдена ВНУТРИ книги — "
              "открывать `highlights` через epav-id")
    print("\n# --fulltext ищет по слою OCR всей коллекции — медленно, но достаёт "
          "фамилию внутри книги, а не только в заголовке")


def cmd_epav_id(a):
    st, hd, raw = get(EPAV + "/findById?" + urllib.parse.urlencode({"id": a.ident}))
    print(f"# HTTP {st}")
    print(raw.decode("utf-8", "replace")[:4000])
    print("\n# ⚠️ пустая карточка ≠ «фонда нет». Проверьте тем же запросом "
          "ЗАВЕДОМО оцифрованное дело — иначе пустой ответ ничего не доказывает")


# ----------------------------------------------------------------- POST
def cmd_post(a):
    """Поиск через POST. Тело берётся как есть — его форму выясняют по бандлу сайта."""
    hdrs = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
    if a.referer:
        hdrs["Referer"] = a.referer
    else:                       # многие шлюзы отвечают 403 без Referer со своего же сайта
        u = urllib.parse.urlsplit(a.url)
        hdrs["Referer"] = f"{u.scheme}://{u.netloc}/"
    body = a.json
    if body.startswith("@"):
        body = open(body[1:], encoding="utf-8").read()
    st, hd, raw = get(a.url, data=body.encode(), headers=hdrs, insecure=a.insecure)
    print(f"# HTTP {st}  {a.url}")
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        print(raw.decode("utf-8", "replace")[:2000]); return
    hits = d.get("hits") if isinstance(d, dict) else None
    if isinstance(hits, dict):                      # ответ в стиле Elasticsearch
        total = hits.get("total")
        if isinstance(total, dict):
            total = total.get("value")
        print(f"# найдено: {total}")
        for h in hits.get("hits", [])[:20]:
            s = h.get("_source", {})
            keep = {k: v for k, v in s.items() if v not in (None, "", [], {})}
            head = " · ".join(f"{k}={keep[k]}" for k in list(keep)[:6])
            print(f"{h.get('_type','')} {h.get('_id','')}  {head[:170]}")
        print("# поля записи смотреть целиком: они содержат шифр и МЕСТО РОЖДЕНИЯ")
    else:
        print(json.dumps(d, ensure_ascii=False, indent=1)[:3000])


# ------------------------------------------------------------------ OAI
def cmd_oai(a):
    """OAI-PMH — недооценённый вход: чистый GET, без ключа и без POST.
    Verb: Identify · ListSets · ListMetadataFormats · ListIdentifiers · GetRecord."""
    q = {"verb": a.verb}
    if a.verb in ("ListIdentifiers", "ListRecords"):
        q["metadataPrefix"] = a.prefix
        if a.set:
            q["set"] = a.set
        if a.dt_from:
            q["from"] = a.dt_from
    if a.verb == "GetRecord":
        q["metadataPrefix"] = a.prefix
        q["identifier"] = a.ident
    url = a.base.rstrip("/") + "?" + urllib.parse.urlencode(q)
    st, hd, raw = get(url, insecure=a.insecure)
    x = raw.decode("utf-8", "replace")
    print(f"# HTTP {st}  {url}")
    err = re.findall(r"<error[^>]*>(.*?)</error>", x, re.S)
    if err:
        print("ОШИБКА OAI:", err[0])
        print("# частая причина: метаданный формат не поддержан — спросить ListMetadataFormats")
        return
    if a.verb == "ListSets":
        for spec, name in re.findall(r"<setSpec>(.*?)</setSpec>\s*<setName>(.*?)</setName>", x, re.S):
            print(f"{spec:38} {name}")
    elif a.verb in ("ListIdentifiers", "ListRecords"):
        for i in re.findall(r"<identifier>(.*?)</identifier>", x)[:200]:
            print(i)
        tok = re.findall(r"<resumptionToken[^>]*>(.*?)</resumptionToken>", x)
        if tok and tok[0]:
            print(f"# продолжение: ?verb={a.verb}&resumptionToken={tok[0]}")
    else:
        print(re.sub(r">\s*<", ">\n<", x)[:4000])


# -------------------------------------------------------------- wayback
def cmd_cdx(a):
    q = {"url": a.domain, "matchType": "domain", "output": "json",
         "collapse": "urlkey", "limit": str(a.limit)}
    if a.filter:
        q["filter"] = "urlkey:.*" + re.escape(a.filter) + ".*"
    url = "http://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(q)
    st, hd, raw = get(url)
    rows = json.loads(raw or b"[]")
    print(f"# HTTP {st}  найдено: {max(0, len(rows)-1)}")
    for r in rows[1:]:
        ts, orig = r[1], r[2]
        print(f"https://web.archive.org/web/{ts}id_/{orig}")
    print("\n# matchType=domain достаёт файлы, ссылок на которые на сайте уже не было")


# ---------------------------------------------------------------- probe
def cmd_probe(a):
    for url in a.urls:
        for insecure in (False, True):
            try:
                st, hd, raw = get(url, insecure=insecure, timeout=25)
                tag = " (только с отключённой проверкой сертификата!)" if insecure else ""
                print(f"{url}\n  HTTP {st}  {hd.get('Content-Type','')}  "
                      f"{len(raw)} байт{tag}")
                break
            except Exception as e:
                if insecure:
                    print(f"{url}\n  НЕДОСТУПЕН: {e}")
                    print("  проверить: страна IP · сертификат · требуется ли логин")
    print("\n⚠️ Один неудачный запрос — не доказательство недоступности (PITFALLS.md).")


def main():
    ap = argparse.ArgumentParser(prog="archnet", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--insecure", action="store_true",
                    help="не проверять TLS-сертификат (у части архивов своя цепочка)")
    ap.add_argument("--socks", metavar="HOST:PORT",
                    help="слать запросы через SOCKS5 (например, 127.0.0.1:11080 от "
                         "`ssh -D 11080 -N узел-в-стране`) — обход блокировки по географии")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("af"); p.add_argument("base"); p.add_argument("query")
    p.add_argument("--digital", action="store_true")
    p.add_argument("--from", dest="dt_from"); p.add_argument("--to", dest="dt_to")
    p.set_defaults(f=cmd_af)

    p = sub.add_parser("kaisa"); p.add_argument("base"); p.add_argument("query")
    p.set_defaults(f=cmd_kaisa)

    p = sub.add_parser("epav"); p.add_argument("query")
    p.add_argument("--fulltext", action="store_true"); p.add_argument("--provider")
    p.add_argument("--page", type=int, default=0); p.add_argument("--size", type=int, default=20)
    p.set_defaults(f=cmd_epav)

    p = sub.add_parser("epav-id"); p.add_argument("ident"); p.set_defaults(f=cmd_epav_id)

    p = sub.add_parser("oai"); p.add_argument("base"); p.add_argument("verb")
    p.add_argument("--set"); p.add_argument("--prefix", default="oai_edm")
    p.add_argument("--id", dest="ident"); p.add_argument("--from", dest="dt_from")
    p.set_defaults(f=cmd_oai)

    p = sub.add_parser("post"); p.add_argument("url")
    p.add_argument("--json", required=True, help="тело запроса (или @файл)")
    p.add_argument("--referer")
    p.set_defaults(f=cmd_post)

    p = sub.add_parser("cdx"); p.add_argument("domain")
    p.add_argument("--filter"); p.add_argument("--limit", type=int, default=200)
    p.set_defaults(f=cmd_cdx)

    p = sub.add_parser("probe"); p.add_argument("urls", nargs="+"); p.set_defaults(f=cmd_probe)

    a = ap.parse_args()
    if not hasattr(a, "insecure"):
        a.insecure = False
    if getattr(a, "socks", None):
        set_socks(a.socks, a.insecure)
    a.f(a)


if __name__ == "__main__":
    main()
