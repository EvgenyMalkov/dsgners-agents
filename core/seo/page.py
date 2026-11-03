"""Скачать страницу и проверить её SEO. Без ИИ — чистые правила, поэтому дёшево и предсказуемо.

check_url(url) → PageReport: что на странице есть (title, description, H1, canonical, OG, JSON-LD, ссылки)
и список проблем [(уровень, код, текст)]. Уровни: critical — страница выпадет или уже выпала из поиска;
warning — теряем трафик или красоту сниппета; info — можно улучшить.
"""
import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urljoin, urldefrag, urlparse

import aiohttp

UA = "Mozilla/5.0 (compatible; dsgners-seo-bot/1.0; +https://dsgners.ru)"

TITLE_MIN, TITLE_MAX = 25, 70
DESC_MIN, DESC_MAX = 70, 170
THIN_WORDS = 250
SITEMAP_TIMEOUT = 120   # секунд на один sitemap-файл (секции на тысячи адресов генерируются долго)
SITEMAP_RETRY_PAUSE = 10


LABELS = {
    "unreachable": "Не открывается", "5xx": "Ошибка сервера 5xx", "4xx": "Ошибка 4xx (404 и т.п.)",
    "noindex": "Закрыто от индексации (noindex)", "no_title": "Нет title", "redirect_chain": "Цепочки редиректов",
    "temp_redirect": "Временные редиректы вместо 301", "canonical_other": "canonical ведёт на другой адрес",
    "no_canonical": "Нет canonical", "title_short": "Короткий title", "title_long": "Длинный title",
    "no_description": "Нет meta description", "desc_short": "Короткий description", "desc_long": "Длинный description",
    "no_h1": "Нет H1", "many_h1": "Несколько H1", "title_eq_h1": "title = H1", "no_og_image": "Нет og:image",
    "no_og_title": "Нет og:title", "jsonld_broken": "Битая JSON-LD разметка",
    "no_article_schema": "Нет разметки Article у статей", "no_breadcrumbs": "Нет разметки хлебных крошек",
    "thin": "Мало текста", "few_links": "Мало внутренних ссылок", "img_no_alt": "Картинки без alt",
    "slow": "Медленный ответ (>3 с)", "no_lang": "Не указан язык страницы",
}


@dataclass
class Fetched:
    url: str                 # запрошенный адрес
    final: str               # куда привели редиректы
    status: int              # код последнего ответа (0 — не ответил)
    chain: list = field(default_factory=list)   # [(код, адрес)] промежуточных редиректов
    headers: dict = field(default_factory=dict)
    html: str = ""
    ms: int = 0
    error: str = ""


async def fetch(url: str, session: aiohttp.ClientSession | None = None, max_bytes=4_000_000,
                max_hops=6, timeout=25) -> Fetched:
    """GET с ручными редиректами — чтобы видеть цепочки и коды каждого шага."""
    own = session is None
    if own:
        session = aiohttp.ClientSession(headers={"User-Agent": UA},
                                        timeout=aiohttp.ClientTimeout(total=timeout))
    res = Fetched(url=url, final=url, status=0)
    t = time.monotonic()
    cur = url
    try:
        for _ in range(max_hops + 1):
            async with session.get(cur, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                    res.chain.append((r.status, cur))
                    cur = urljoin(cur, r.headers["Location"])
                    continue
                res.status = r.status
                res.final = str(r.url)
                res.headers = {k.lower(): v for k, v in r.headers.items()}
                if "html" in res.headers.get("content-type", "") or "xml" in res.headers.get("content-type", "") \
                        or "text" in res.headers.get("content-type", ""):
                    raw = await r.content.read(max_bytes)
                    res.html = raw.decode(r.charset or "utf-8", errors="ignore")
                break
        else:
            res.error = f"больше {max_hops} редиректов"
    except Exception as e:
        res.error = (f"тайм-аут {timeout} с" if isinstance(e, asyncio.TimeoutError)
                     else f"{type(e).__name__}: {e}"[:200])
    finally:
        res.ms = int((time.monotonic() - t) * 1000)
        if own:
            await session.close()
    return res


# ---------- разбор HTML ----------

def strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|template|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def attr(tag: str, name: str) -> str:
    m = re.search(rf'(?i)[\s<]{name}\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag)
    if not m:
        return ""
    return unescape((m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None else m.group(4) or "").strip())


def norm(url: str) -> str:
    """Адрес без #якоря и без хвостового слэша (кроме корня) — для сравнения."""
    u = urldefrag(url)[0]
    p = urlparse(u)
    path = p.path if p.path in ("", "/") else p.path.rstrip("/")
    return f"{p.scheme}://{p.netloc.lower()}{path or '/'}" + (f"?{p.query}" if p.query else "")


@dataclass
class PageReport:
    url: str
    final: str = ""
    status: int = 0
    ms: int = 0
    chain: list = field(default_factory=list)
    error: str = ""
    title: str = ""
    description: str = ""
    h1: list = field(default_factory=list)
    h2: list = field(default_factory=list)
    canonical: str = ""
    robots: str = ""          # meta robots + X-Robots-Tag
    lang: str = ""
    og: dict = field(default_factory=dict)
    schema_types: list = field(default_factory=list)
    words: int = 0
    images: int = 0
    images_no_alt: list = field(default_factory=list)
    internal_links: list = field(default_factory=list)
    external_links: int = 0
    issues: list = field(default_factory=list)   # [(level, code, text)]

    @property
    def noindex(self) -> bool:
        return "noindex" in self.robots.lower()

    def add(self, level: str, code: str, text: str):
        self.issues.append((level, code, text))

    def summary(self) -> str:
        """Человекочитаемая сводка для агента и отчётов."""
        out = [f"URL: {self.final or self.url} — HTTP {self.status}, {self.ms} мс"
               + (f", редиректы: {' → '.join(str(c) for c, _ in self.chain)}" if self.chain else "")]
        if self.error:
            out.append(f"Ошибка загрузки: {self.error}")
        out += [
            f"title ({len(self.title)} симв.): {self.title or '—'}",
            f"description ({len(self.description)} симв.): {self.description or '—'}",
            f"H1 ({len(self.h1)}): " + (" | ".join(self.h1) or "—"),
            "H2: " + (" | ".join(self.h2[:12]) or "—"),
            f"canonical: {self.canonical or '—'}; robots: {self.robots or 'нет запретов'}; lang: {self.lang or '—'}",
            "OpenGraph: " + (", ".join(f"{k}={v[:60]}" for k, v in self.og.items()) or "нет"),
            "Разметка schema.org: " + (", ".join(self.schema_types) or "нет"),
            f"Слов: ~{self.words}; картинок: {self.images}, без alt: {len(self.images_no_alt)}; "
            f"внутренних ссылок: {len(self.internal_links)}, внешних: {self.external_links}",
        ]
        if self.issues:
            mark = {"critical": "‼️", "warning": "⚠️", "info": "•"}
            out.append("Проблемы:\n" + "\n".join(f"  {mark.get(l, '•')} {t}" for l, _, t in self.issues))
        else:
            out.append("Проблем не найдено.")
        return "\n".join(out)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["internal_links"] = d["internal_links"][:300]
        return d


def _schema_types(html: str) -> list[str]:
    types: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            t = o.get("@type")
            if isinstance(t, str):
                types.append(t)
            elif isinstance(t, list):
                types.extend(str(x) for x in t)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html):
        try:
            walk(json.loads(block.strip()))
        except Exception:
            types.append("ОШИБКА-JSON-LD")
    types += re.findall(r'(?i)itemtype\s*=\s*["\']https?://schema\.org/(\w+)', html)
    seen, out = set(), []
    for t in types:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def analyze(f: Fetched, is_article: bool = False) -> PageReport:
    rep = PageReport(url=f.url, final=f.final, status=f.status, ms=f.ms, chain=f.chain, error=f.error)
    if f.error or f.status == 0:
        rep.add("critical", "unreachable", f"Страница не открылась: {f.error or 'нет ответа'}")
        return rep
    if f.status >= 500:
        rep.add("critical", "5xx", f"Ошибка сервера HTTP {f.status}")
        return rep
    if f.status >= 400:
        rep.add("critical", "4xx", f"Страница отдаёт HTTP {f.status}")
        return rep
    if len(f.chain) > 1:
        rep.add("warning", "redirect_chain", f"Цепочка из {len(f.chain)} редиректов: "
                + " → ".join(u for _, u in f.chain) + f" → {f.final}")
    if f.chain and f.chain[0][0] in (302, 303, 307):
        rep.add("info", "temp_redirect", f"Временный редирект {f.chain[0][0]} — для постоянного переезда нужен 301")

    html = f.html
    host = urlparse(f.final).netloc
    head = html[: (m.start() if (m := re.search(r"(?i)<body", html)) else 200_000)]

    rep.title = strip_html(m.group(1)) if (m := re.search(r"(?is)<title[^>]*>(.*?)</title>", head)) else ""
    metas: dict[str, str] = {}
    for tag in re.findall(r"(?is)<meta\b[^>]*>", head):
        key = (attr(tag, "name") or attr(tag, "property")).lower()
        if key:
            metas.setdefault(key, attr(tag, "content"))
    rep.description = metas.get("description", "")
    rep.og = {k: v for k, v in metas.items() if k.startswith("og:") or k.startswith("twitter:")}
    robots = [metas.get("robots", ""), metas.get("yandex", ""), f.headers.get("x-robots-tag", "")]
    rep.robots = ", ".join(r for r in robots if r)
    for tag in re.findall(r"(?is)<link\b[^>]*>", head):
        if "canonical" in attr(tag, "rel").lower().split():
            rep.canonical = urljoin(f.final, attr(tag, "href"))
    rep.lang = attr(m.group(0), "lang") if (m := re.search(r"(?is)<html\b[^>]*>", html)) else ""
    rep.h1 = [strip_html(t)[:200] for t in re.findall(r"(?is)<h1\b[^>]*>(.*?)</h1>", html)]
    rep.h2 = [strip_html(t)[:150] for t in re.findall(r"(?is)<h2\b[^>]*>(.*?)</h2>", html)]
    rep.schema_types = _schema_types(html)
    body = m.group(1) if (m := re.search(r"(?is)<(?:article|main)\b[^>]*>(.*)</(?:article|main)>", html)) else html
    rep.words = len(strip_html(body).split())
    imgs = re.findall(r"(?is)<img\b[^>]*>", body)
    rep.images = len(imgs)
    for i in imgs:
        src = attr(i, "src") or attr(i, "data-src")
        if not attr(i, "alt") and src and not src.startswith("data:"):
            rep.images_no_alt.append(urljoin(f.final, src))
    seen = set()
    for a in re.findall(r"(?is)<a\b[^>]*>", html):
        href = attr(a, "href")
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        u = norm(urljoin(f.final, href))
        if urlparse(u).netloc == host:
            if u not in seen:
                seen.add(u)
                rep.internal_links.append(u)
        else:
            rep.external_links += 1

    # --- правила
    if rep.noindex:
        rep.add("critical", "noindex", f"Страница закрыта от индексации ({rep.robots})")
    if rep.canonical and norm(rep.canonical) != norm(f.final):
        rep.add("warning", "canonical_other", f"canonical указывает на другой адрес: {rep.canonical} — "
                "поиск будет показывать его, а не эту страницу")
    if not rep.canonical:
        rep.add("info", "no_canonical", "Нет canonical")
    if not rep.title:
        rep.add("critical", "no_title", "Нет title")
    elif len(rep.title) < TITLE_MIN:
        rep.add("warning", "title_short", f"title короткий ({len(rep.title)} симв.) — мало ключевых слов в сниппете")
    elif len(rep.title) > TITLE_MAX:
        rep.add("info", "title_long", f"title длинный ({len(rep.title)} симв.) — обрежется в выдаче")
    if not rep.description:
        rep.add("warning", "no_description", "Нет meta description — поисковик сам соберёт сниппет из текста")
    elif len(rep.description) < DESC_MIN:
        rep.add("info", "desc_short", f"description короткий ({len(rep.description)} симв.)")
    elif len(rep.description) > DESC_MAX:
        rep.add("info", "desc_long", f"description длинный ({len(rep.description)} симв.)")
    if not rep.h1:
        rep.add("warning", "no_h1", "Нет H1")
    elif len(rep.h1) > 1:
        rep.add("info", "many_h1", f"H1 несколько ({len(rep.h1)})")
    if rep.title and rep.h1 and rep.title.strip().lower() == rep.h1[0].strip().lower() and is_article:
        rep.add("info", "title_eq_h1", "title совпадает с H1 — можно сделать title под запрос, а H1 — для читателя")
    if not rep.og.get("og:image"):
        rep.add("warning", "no_og_image", "Нет og:image — некрасивое превью в Telegram/VK и нет шанса на Discover")
    if not rep.og.get("og:title"):
        rep.add("info", "no_og_title", "Нет og:title")
    if "ОШИБКА-JSON-LD" in rep.schema_types:
        rep.add("warning", "jsonld_broken", "Битая JSON-LD разметка (не парсится)")
    if is_article:
        if not {"Article", "BlogPosting", "NewsArticle", "TechArticle"} & set(rep.schema_types):
            rep.add("warning", "no_article_schema", "Нет разметки Article/NewsArticle — хуже сниппет и шансы на нейроответ")
        if "BreadcrumbList" not in rep.schema_types:
            rep.add("info", "no_breadcrumbs", "Нет разметки хлебных крошек (BreadcrumbList)")
        if rep.words < THIN_WORDS:
            rep.add("warning", "thin", f"Мало текста (~{rep.words} слов) — риск «малополезной страницы»")
        if len(rep.internal_links) < 5:
            rep.add("info", "few_links", "Мало внутренних ссылок со страницы")
    if rep.images_no_alt:
        rep.add("warning" if is_article else "info", "img_no_alt", f"Картинок без alt: {len(rep.images_no_alt)}")
    if f.ms > 3000:
        rep.add("warning", "slow", f"Медленный ответ: {f.ms} мс")
    if not rep.lang:
        rep.add("info", "no_lang", "Не указан язык <html lang>")
    return rep


async def check_url(url: str, session=None, is_article: bool = False) -> PageReport:
    return analyze(await fetch(url, session), is_article)


# ---------- sitemap ----------

@dataclass
class SitemapResult:
    urls: dict = field(default_factory=dict)      # {адрес страницы: (lastmod, sitemap-файл, откуда адрес)}
    failed: list = field(default_factory=list)    # [(sitemap-файл, причина)] — не загрузились даже после повторов
    files: list = field(default_factory=list)     # sitemap-файлы, которые прочитаны успешно


async def _fetch_sitemap(url: str, session, tries=3) -> Fetched:
    """Sitemap с повторами: большой файл секции может не успеть за один раз — это не повод считать его пустым."""
    import asyncio
    f = Fetched(url=url, final=url, status=0)
    for i in range(tries):
        f = await fetch(url, session, max_bytes=60_000_000, timeout=SITEMAP_TIMEOUT)
        if f.status == 200 and f.html and ("<urlset" in f.html or "<sitemapindex" in f.html):
            return f
        if 400 <= f.status < 500 and f.status != 429:
            break                      # 404/403 — повтор не поможет
        if i < tries - 1:
            await asyncio.sleep(SITEMAP_RETRY_PAUSE * (i + 1))
    if f.status == 200 and not f.error:
        f.error = "ответ без <urlset>/<sitemapindex> (обрезан или не XML)"
    return f


async def read_sitemaps(urls: list[str], session=None, limit=200_000) -> SitemapResult:
    """Читает sitemap и индексы sitemap-ов (вложенность до 3). Неудачные секции попадают в failed, а не «пустые»."""
    res = SitemapResult()
    queue = [(u, 0) for u in urls]
    seen = set()
    while queue and len(res.urls) < limit:
        sm, depth = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        f = await _fetch_sitemap(sm, session)
        if f.status != 200 or f.error:
            res.failed.append((sm, f.error or f"HTTP {f.status}"))
            continue
        res.files.append(sm)
        xml = f.html
        if "<sitemapindex" in xml and depth < 3:
            queue += [(unescape(u.strip()), depth + 1) for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml)]
            continue
        for block in re.findall(r"(?is)<url>(.*?)</url>", xml):
            loc = re.search(r"(?is)<loc>\s*([^<]+?)\s*</loc>", block)
            if not loc:
                continue
            lm = re.search(r"(?is)<lastmod>\s*([^<]+?)\s*</lastmod>", block)
            res.urls[norm(unescape(loc.group(1)))] = (lm.group(1).strip() if lm else "", sm)
    return res


def sitemaps_from_robots(robots: str) -> list[str]:
    return [s.strip() for s in re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)]


def robots_blocks_all(robots: str) -> bool:
    """robots.txt закрывает весь сайт для Яндекса или для всех роботов?"""
    agent, blocked = None, False
    for line in robots.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            agent = v.lower()
        elif k == "disallow" and v == "/" and agent in ("*", "yandex", "googlebot"):
            blocked = True
    return blocked
