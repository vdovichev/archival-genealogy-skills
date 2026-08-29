#!/usr/bin/env python3
"""Шлюз для браузерного чата: принимает публичный запрос, ходит в архив из России,
отдаёт ответ. ТОЛЬКО ЧТЕНИЕ, только GET, только домены из белого списка.

    GET /g/<ТОКЕН>?url=https://<разрешённый-домен>/...

Защита (без неё это открытый прокси, который найдут за часы):
  * секретный токен в пути; без него — 404, без подсказок;
  * белый список доменов; произвольный URL не пропускается;
  * только GET и только http/https; порт в URL запрещён;
  * ограничение размера ответа и числа запросов в минуту;
  * редиректы разрешены только внутри белого списка;
  * в лог не пишется тело ответа.
Стандартная библиотека, без зависимостей.
"""
import http.server, json, os, re, socket, ssl, sys, threading, time
import urllib.error, urllib.parse, urllib.request

TOKEN   = os.environ.get("GW_TOKEN", "")
PORT    = int(os.environ.get("GW_PORT", "80"))
BUNDLE  = os.environ.get("GW_CA", "/opt/gw/ru-ca-bundle.pem")
MAXSIZE = 12 * 1024 * 1024
RATE    = 60                      # запросов в минуту суммарно
TIMEOUT = 45

ALLOW = {
    # архивные АИС и порталы
    "rusarchives.ru", "guides.rusarchives.ru", "unsecret.rusarchives.ru",
    "cavalier.rusarchives.ru", "portal.rusarchives.ru", "rgada.ru",
    "spbarchives.ru", "archive.pskov.ru", "pokolenia.permkrai.ru",
    "gato.tularegion.ru", "cgamos.ru", "kosarchive.ru", "yararchive.ru",
    "tambovarchiv.ru", "kaisa.tambovarchiv.ru", "ivarh.ru", "vlarhiv.ru",
    "arhiv-pnz.ru", "archive.admoblkaluga.ru", "archive-bryansk.ru",
    "gaso.admin-smolensk.ru", "gaorel.ru", "e-mordovia.ru", "dvinaland.ru",
    # именные базы и библиотеки
    "gwar.mil.ru", "pamyat-naroda.ru", "podvignaroda.ru", "obd-memorial.ru",
    "moypolk.ru", "rf-poisk.ru", "soldat.ru", "ru.openlist.wiki",
    "sic.rgantd.ru", "rusneb.ru", "kp.rusneb.ru", "elib.shpl.ru", "rsl.ru",
    "nekrasovka.ru", "imena.nekrasovka.ru", "goskatalog.ru", "russiainphoto.ru",
    "rusalbom.ru", "pastvu.com", "prozhito.org", "stolypin.ru", "vostlit.info",
    "rodnaya-vyatka.ru", "census1710.narod.ru", "vgd.ru", "yar-genealogy.ru",
    "familio.org", "dokst.ru",
}

_hits = []
_lock = threading.Lock()


def normalize(u):
    """Кириллица (и любой не-ASCII) в пути и запросе должна уехать в percent-encoding,
    иначе urllib падает с UnicodeEncodeError, а снаружи это выглядит как 502.
    Уже закодированные %XX сохраняются — safe включает '%'."""
    p = urllib.parse.urlsplit(u)
    path = urllib.parse.quote(p.path, safe="/%:@!$&'()*+,;=~")
    query = urllib.parse.quote(p.query, safe="%=&:/?@!$'()*+,;~[]")
    host = p.netloc.encode("idna").decode() if any(ord(c) > 127 for c in p.netloc) else p.netloc
    return urllib.parse.urlunsplit((p.scheme, host, path, query, ""))


def allowed(host):
    host = host.lower().split(":")[0]
    return host in ALLOW or any(host.endswith("." + d) for d in ALLOW)


def rate_ok():
    now = time.time()
    with _lock:
        _hits[:] = [t for t in _hits if now - t < 60]
        if len(_hits) >= RATE:
            return False
        _hits.append(now)
        return True


def ctx():
    c = ssl.create_default_context(cafile=BUNDLE if os.path.exists(BUNDLE) else None)
    return c


# ------------------------------------------- переходник GET→POST для базы ПМВ
# Поиск на портале Первой мировой устроен через POST к его внутреннему шлюзу
# (builderType=Heroes), а чат умеет только GET. Переходник строит запрос САМ
# из разрешённых полей — произвольное тело запроса наружу не пропускается.
GWAR_URL = "https://gwar.mil.ru/gt_data/?builder=Heroes"
GWAR_FIELDS = ("last_name", "first_name", "middle_name", "birth_place",
               "birth_place_gubernia", "birth_place_uezd", "birth_place_volost",
               "rank", "military_unit_name", "award_name", "camp_name")
GWAR_OUT = ("last_name", "first_name", "middle_name", "rank", "birth_place",
            "birth_place_gubernia", "birth_place_uezd", "birth_place_volost",
            "military_unit_name", "person_type", "doc_type", "document_date",
            "award_name", "camp_name", "camp_location", "archive_short",
            "fund", "inventory_num", "deal", "box")


def gwar_search(params):
    q = {k: v[0] for k, v in params.items() if k in GWAR_FIELDS and v and v[0].strip()}
    if not q:
        return 400, {"error": "нужен хотя бы один параметр",
                     "доступны": list(GWAR_FIELDS)}
    try:
        size = min(int(params.get("size", ["10"])[0]), 25)
        frm = min(int(params.get("from", ["0"])[0]), 5000)
    except ValueError:
        return 400, {"error": "size и from должны быть числами"}
    body = json.dumps({"builderType": "Heroes", "queryFields": q, "filterFields": {},
                       "indices": ["gwar"], "entities": [], "from": frm, "size": size},
                      ensure_ascii=False).encode()
    req = urllib.request.Request(GWAR_URL, data=body, headers={
        "Content-Type": "application/json",
        "Referer": "https://gwar.mil.ru/heroes/",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx()))
    with opener.open(req, timeout=TIMEOUT) as r:
        d = json.loads(r.read(MAXSIZE))
    hits = d.get("hits", {})
    out = []
    for h in hits.get("hits", []):
        s = h.get("_source", {})
        rec = {k: s[k] for k in GWAR_OUT if s.get(k)}
        rec["карточка"] = f"https://gwar.mil.ru/heroes/{h.get('_type','')}{s.get('id','')}/"
        out.append(rec)
    total = hits.get("total")
    if isinstance(total, dict):
        total = total.get("value")
    return 200, {"запрос": q, "всего": total, "показано": len(out),
                 "от": frm, "записи": out,
                 "подсказка": "сузить поиск: birth_place_gubernia, birth_place_uezd, "
                              "birth_place_volost, first_name, military_unit_name"}


class Opener(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(urllib.parse.urlparse(newurl).hostname or ""):
            raise urllib.error.HTTPError(newurl, 403, "редирект за пределы белого списка", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class H(http.server.BaseHTTPRequestHandler):
    server_version = "gw"
    sys_version = ""

    def log_message(self, fmt, *args):           # без тел и без query с токеном
        path = self.path.split("?")[0]
        sys.stderr.write(f"{time.strftime('%F %T')} {self.address_string()} {path} {args[1] if len(args)>1 else ''}\n")

    def deny(self, code=404):
        self.send_response(code); self.send_header("Content-Length", "0"); self.end_headers()

    def json_out(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if TOKEN and u.path == f"/gwar/{TOKEN}":
            if not rate_ok():
                return self.deny(429)
            try:
                code, obj = gwar_search(urllib.parse.parse_qs(u.query))
            except Exception as e:
                code, obj = 502, {"error": type(e).__name__, "detail": str(e)[:200]}
            return self.json_out(code, obj)
        if not TOKEN or u.path != f"/g/{TOKEN}":
            return self.deny()
        target = urllib.parse.parse_qs(u.query).get("url", [""])[0]
        t = urllib.parse.urlparse(target)
        if t.scheme not in ("http", "https") or not t.hostname or t.port:
            return self.deny(400)
        if not allowed(t.hostname):
            body = json.dumps({"error": "домен не в белом списке", "host": t.hostname},
                              ensure_ascii=False).encode()
            self.send_response(403); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if not rate_ok():
            return self.deny(429)
        req = urllib.request.Request(normalize(target), headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.8"})
        opener = urllib.request.build_opener(Opener(), urllib.request.HTTPSHandler(context=ctx()))
        try:
            with opener.open(req, timeout=TIMEOUT) as r:
                data = r.read(MAXSIZE + 1)
                ct = r.headers.get("Content-Type", "application/octet-stream")
                code = r.status
        except urllib.error.HTTPError as e:
            data, ct, code = e.read(MAXSIZE + 1), e.headers.get("Content-Type", "text/plain"), e.code
        except Exception as e:
            body = json.dumps({"error": type(e).__name__, "detail": str(e)[:200]},
                              ensure_ascii=False).encode()
            self.send_response(502); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if len(data) > MAXSIZE:
            return self.deny(413)
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Gateway-Source", t.hostname)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self): self.deny(405)
    def do_PUT(self): self.deny(405)
    def do_DELETE(self): self.deny(405)


class Srv(http.server.ThreadingHTTPServer):
    daemon_threads = True
    address_family = socket.AF_INET


if __name__ == "__main__":
    if not TOKEN or len(TOKEN) < 24:
        sys.exit("GW_TOKEN не задан или короче 24 символов")
    print(f"шлюз на :{PORT}, доменов в списке: {len(ALLOW)}, "
          f"бандл: {'есть' if os.path.exists(BUNDLE) else 'нет'}", flush=True)
    Srv(("0.0.0.0", PORT), H).serve_forever()
