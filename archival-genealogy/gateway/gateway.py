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
import base64, binascii, http.server, json, os, re, socket, ssl, sys, threading, time
import urllib.error, urllib.parse, urllib.request

TOKEN   = os.environ.get("GW_TOKEN", "")
PORT    = int(os.environ.get("GW_PORT", "80"))
BUNDLE  = os.environ.get("GW_CA", "/opt/gw/ru-ca-bundle.pem")
BASE    = os.environ.get("GW_BASE", "").rstrip("/")   # публичный адрес шлюза:
# ссылки в ответе делаем АБСОЛЮТНЫМИ — часть загрузчиков берёт только те адреса,
# которые встретились в тексте целиком, и относительный /g/... до них не доходит
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


# ------------------------------------------------- переписывание ссылок в ответе
# Ассистент в браузере умеет загружать только адреса, которые дословно встретились
# ему в переписке или в уже полученном ответе, — собрать новый он не может.
# Поэтому страница бесполезна, если ссылки в ней ведут «наружу»: относительный
# /awards загрузчик разрешит против НАШЕГО хоста и получит 404. Переписываем каждую
# ссылку в полный вид /g/<секрет>/<base64url абсолютного адреса>, и тогда навигация
# разворачивается сама: одна входная страница даёт доступ ко всему разделу.
LINK_RE = re.compile(rb'(<a\b[^>]*?\bhref\s*=\s*)(["\'])(.*?)\2', re.I | re.S)
IMG_RE = re.compile(rb'(<(?:img|script|link)\b[^>]*?\b(?:src|href)\s*=\s*)(["\'])(.*?)\2', re.I | re.S)


def b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def gw_link(absolute):
    return f"{BASE}/g/{TOKEN}/{b64(absolute)}"


def rewrite_html(body, base_url):
    def fix_a(m):
        raw = m.group(3).decode("utf-8", "replace").strip()
        if not raw or raw.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            return m.group(0)
        absolute = urllib.parse.urljoin(base_url, raw)
        pr = urllib.parse.urlparse(absolute)
        if pr.scheme not in ("http", "https") or not pr.hostname or not allowed(pr.hostname):
            return m.group(0)                      # чужой домен оставляем как есть
        return m.group(1) + m.group(2) + gw_link(absolute).encode() + m.group(2)

    def fix_asset(m):
        # картинки и стили НЕ гоним через шлюз: они съедали бы лимит запросов.
        # Просто делаем адрес абсолютным, чтобы он не бил в наш хост.
        raw = m.group(3).decode("utf-8", "replace").strip()
        if not raw or raw.startswith(("#", "data:", "http://", "https://", "//")):
            return m.group(0)
        return m.group(1) + m.group(2) + urllib.parse.urljoin(base_url, raw).encode() + m.group(2)

    body = LINK_RE.sub(fix_a, body)
    return IMG_RE.sub(fix_asset, body)


# ---------------------------------------------------------------- точка входа
# Одна страница, адрес которой человек даёт ассистенту в переписке. Дальше тот
# ходит ТОЛЬКО по ссылкам отсюда — этого достаточно, чтобы развернуть поиск.
ENTRY = [
    ("Каталоги дел архивов", [
        ("Госархив Тамбовской обл. (движок КАИСА)", "https://kaisa.tambovarchiv.ru/"),
        ("Госархив Ивановской обл.", "https://ivarh.ru/"),
        ("Госархив Владимирской обл.", "https://vlarhiv.ru/"),
        ("Госархив Ярославской обл.", "https://yararchive.ru/"),
        ("Госархив Псковской обл.", "https://archive.pskov.ru/"),
        ("Архивы Санкт-Петербурга", "https://spbarchives.ru/"),
        ("ЦГА Москвы", "https://cgamos.ru/"),
        ("Пермский край: Поколения", "https://pokolenia.permkrai.ru/"),
        ("РГАДА", "https://rgada.ru/"),
        ("Портал «Архивы России»", "https://rusarchives.ru/"),
    ]),
    ("Именные базы", [
        ("Первая мировая: потери и награды", "https://gwar.mil.ru/"),
        ("Георгиевские кавалеры", "https://cavalier.rusarchives.ru/"),
        ("Открытый список (репрессии)", "https://ru.openlist.wiki/"),
        ("Бессмертный полк", "https://www.moypolk.ru/"),
        ("Память народа", "https://pamyat-naroda.ru/"),
    ]),
    ("Книги, справочники, изображения", [
        ("Историческая библиотека (ГПИБ)", "https://elib.shpl.ru/ru/nodes/search"),
        ("Национальная электронная библиотека", "https://rusneb.ru/"),
        ("Госкаталог музейного фонда", "https://goskatalog.ru/"),
        ("Электронекрасовка", "https://electro.nekrasovka.ru/"),
        ("Родная Вятка", "https://rodnaya-vyatka.ru/"),
    ]),
]


def entry_page():
    h = ["<!doctype html><meta charset=utf-8><title>Архивный шлюз</title>",
         "<h1>Архивный шлюз</h1>",
         "<p>Ниже — входы в архивные источники. Ходите по этим ссылкам: они уже "
         "в рабочем виде. Ссылки внутри полученных страниц тоже переписаны, "
         "поэтому навигация работает вглубь.</p>",
         "<p>Поиск по фамилии обычно задаётся параметрами адреса, а их ваш загрузчик "
         "срезает. Если нужна страница результатов — попросите человека прислать "
         "готовую ссылку: он соберёт её одной командой.</p>"]
    for title, items in ENTRY:
        h.append(f"<h2>{title}</h2><ul>")
        for name, url in items:
            h.append(f'<li><a href="{gw_link(url)}">{name}</a></li>')
        h.append("</ul>")
    return "\n".join(h).encode()


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
        card = f"https://gwar.mil.ru/heroes/{h.get('_type','')}{s.get('id','')}/"
        rec["карточка"] = gw_link(card)      # сразу в форме шлюза: иначе не открыть
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

    @staticmethod
    def unpack(seg):
        """Цель приходит хвостом пути, а не параметром: некоторые загрузчики режут
        строку запроса целиком, и до сервера доходит голый /g/<секрет>.
        Понимаем два вида хвоста: base64url (без ? и & вовсе — самый надёжный)
        и обычный адрес как есть."""
        seg = seg.lstrip("/")
        if not seg:
            return ""
        if seg.startswith("http://") or seg.startswith("https://"):
            return seg
        low = seg.lower()
        if low.startswith("http%3a") or low.startswith("https%3a"):
            return urllib.parse.unquote(seg)
        try:
            pad = "=" * (-len(seg) % 4)
            d = base64.urlsafe_b64decode(seg + pad).decode("utf-8")
            return d if d.startswith(("http://", "https://")) else ""
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return ""

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if TOKEN and u.path.startswith(f"/gwar/{TOKEN}"):
            tail = u.path[len(f"/gwar/{TOKEN}"):].lstrip("/")
            if tail:                       # параметры пришли хвостом пути, в base64url
                try:
                    pad = "=" * (-len(tail) % 4)
                    u = u._replace(query=base64.urlsafe_b64decode(tail + pad).decode("utf-8"))
                except Exception:
                    return self.json_out(400, {"error": "хвост пути не разобран",
                                               "ожидается": "base64url от строки параметров"})
            if not rate_ok():
                return self.deny(429)
            try:
                code, obj = gwar_search(urllib.parse.parse_qs(u.query))
            except Exception as e:
                code, obj = 502, {"error": type(e).__name__, "detail": str(e)[:200]}
            return self.json_out(code, obj)

        if TOKEN and u.path.startswith(f"/g/{TOKEN}/"):
            target = self.unpack(u.path[len(f"/g/{TOKEN}"):])
            if u.query:                    # хвост пути + уцелевшая строка запроса
                target += ("&" if "?" in target else "?") + u.query
            return self.fetch(target)

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
        return self.fetch(urllib.parse.parse_qs(u.query).get("url", [""])[0])

    def fetch(self, target):
        if not target:
            body = entry_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if False:
            return self.json_out(400, {
                "error": "адрес не получен",
                "подсказка": "строку запроса режут некоторые загрузчики — передавайте "
                             "цель хвостом пути: /g/<секрет>/<адрес в base64url>"})
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
        if "html" in ct.lower():
            data = rewrite_html(data, target)
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
    print(f"шлюз на :{PORT}, база ссылок: {BASE or '(относительные)'}, "
          f"доменов в списке: {len(ALLOW)}, "
          f"бандл: {'есть' if os.path.exists(BUNDLE) else 'нет'}", flush=True)
    Srv(("0.0.0.0", PORT), H).serve_forever()
