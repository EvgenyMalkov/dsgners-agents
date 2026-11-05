"""Сбор статей: RSS → детали статьи (API площадки, а если он не ответил — сама страница).

Без браузера и логинов. Пауза между запросами к одному сайту — PR_DELAY (2.5 с).
Адреса API площадок неофициальные и иногда меняются — они в .env (PR_HABR_API, PR_VC_API);
/pr_test показывает, что именно сейчас отвечает.
"""
import asyncio
import email.utils
import html as htmllib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlparse

import aiohttp

from core import config

log = logging.getLogger("pr.sources")

UA = "Mozilla/5.0 (compatible; dsgners-pr/1.0; +https://dsgners.ru)"
DELAY = float(config.env("PR_DELAY", "2.5"))
HABR_API = config.env("PR_HABR_API", "https://habr.com/kek/v2/articles/{id}/?fl=ru&hl=ru")
HABR_USER_API = config.env("PR_HABR_USER_API", "https://habr.com/kek/v2/users/{alias}/card?fl=ru&hl=ru")
VC_API = config.env("PR_VC_API", "https://api.vc.ru/v2.31/content?id={id}")

_last_hit: dict[str, float] = {}
_lock = asyncio.Lock()


@dataclass
class Item:
    """Запись из RSS."""
    url: str
    title: str
    author: str = ""
    published: float = 0.0


@dataclass
class Article:
    """Всё, что удалось узнать о статье."""
    url: str
    site: str
    title: str = ""
    author: str = ""
    author_url: str = ""
    author_alias: str = ""
    published: float = 0.0
    text: str = ""
    images: int = 0
    corporate: bool | None = None       # None — площадка не сказала
    translation: bool = False
    hubs: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)          # score, comments, favorites, views
    author_stats: dict = field(default_factory=dict)   # karma, rating, posts, followers
    author_id: str = ""                                 # id автора на площадке (vc)
    links: list = field(default_factory=list)           # ссылки из текста: {"url", "ctx"} — 150 знаков вокруг
    via: str = ""                                       # api | page


# ---------- HTTP ----------

async def fetch(session: aiohttp.ClientSession, url: str, want_json: bool = False):
    host = urlparse(url).netloc
    async with _lock:
        wait = DELAY - (time.time() - _last_hit.get(host, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        _last_hit[host] = time.time()
    async with session.get(url, headers={"User-Agent": UA, "Accept-Language": "ru"},
                           timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        if want_json:
            return json.loads(await r.text())
        return await r.text()


def session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession()


# ---------- RSS ----------

def _ts(s: str) -> float:
    if not s:
        return 0.0
    try:
        return email.utils.parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_rss(xml: str) -> list[Item]:
    """RSS 2.0 и Atom. Автор — dc:creator / author."""
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    out = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        get = {c.tag.split("}")[-1]: c for c in el}
        link = ""
        if "link" in get:
            link = (get["link"].text or get["link"].get("href") or "").strip()
        if not link and "guid" in get:
            link = (get["guid"].text or "").strip()
        title = htmllib.unescape((get.get("title").text if "title" in get else "") or "").strip()
        author = ""
        for k in ("creator", "author"):
            if k in get:
                a = get[k]
                author = (a.text or "").strip() or next((n.text or "" for n in a if n.tag.endswith("name")), "")
                break
        date = ""
        for k in ("pubDate", "published", "updated", "date"):
            if k in get and get[k].text:
                date = get[k].text.strip()
                break
        if link.startswith("http"):
            out.append(Item(url=_clean_url(link), title=title, author=author, published=_ts(date)))
    return out


def _clean_url(u: str) -> str:
    return u.split("?")[0].split("#")[0]


# ---------- помощники ----------

def deep(obj, *keys, default=None):
    """Первое значение по любому из ключей где угодно во вложенном JSON."""
    stack = [obj]
    while stack:
        o = stack.pop(0)
        if isinstance(o, dict):
            for k in keys:
                if k in o and o[k] not in (None, ""):
                    return o[k]
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return default


def html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", h or "")
    h = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>", "\n", h)
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", h))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def extract_links(h: str) -> list[dict]:
    """Ссылки из HTML с текстом вокруг (по нему видно, свой ли это канал: «мой канал», «подписывайтесь»)."""
    out, seen = [], set()
    for m in re.finditer(r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', h or ""):
        url = htmllib.unescape(m.group(1)).strip()
        if not url.startswith(("http", "mailto:")) or url in seen:
            continue
        seen.add(url)
        before = html_to_text(h[max(0, m.start() - 400):m.start()])
        anchor = html_to_text(m.group(2))
        # только то, что относится к этой ссылке: конец предложения перед ней + текст самой ссылки
        before = re.split(r"[.!?\n]\s", before)[-1][-120:]
        out.append({"url": url, "ctx": f"{before} {anchor}".strip()})
    return out[:60]


def count_images(h: str) -> int:
    return len(re.findall(r"(?i)<img\b", h or ""))


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _id(url: str) -> str:
    m = re.search(r"/(\d{3,})(?:[-/]|$)", url)
    return m.group(1) if m else ""


# ---------- Хабр ----------

async def habr_article(s: aiohttp.ClientSession, item: Item) -> Article:
    a = Article(url=item.url, site="habr", title=item.title, author=item.author, published=item.published)
    a.corporate = "/companies/" in item.url
    aid = _id(item.url)
    try:
        j = await fetch(s, HABR_API.format(id=aid), want_json=True)
        a.via = "api"
        a.title = htmllib.unescape(re.sub(r"<[^>]+>", "", j.get("titleHtml") or j.get("title") or a.title))
        au = j.get("author") or {}
        a.author_alias = au.get("alias") or ""
        a.author = au.get("fullname") or a.author_alias or a.author
        if a.author_alias:
            a.author_url = f"https://habr.com/ru/users/{a.author_alias}/"
        a.published = _ts(j.get("timePublished") or "") or a.published
        body = j.get("textHtml") or ""
        a.text, a.images, a.links = html_to_text(body), count_images(body), extract_links(body)
        if j.get("isCorporative") is not None:
            a.corporate = bool(j["isCorporative"]) or a.corporate
        a.translation = any((l.get("type") if isinstance(l, dict) else l) in ("translation", "Перевод")
                            for l in (j.get("postLabels") or []))
        a.hubs = [h.get("title") or h.get("alias") for h in (j.get("hubs") or []) if isinstance(h, dict)]
        st = j.get("statistics") or {}
        a.stats = {"score": _num(st.get("score")), "comments": _num(st.get("commentsCount")),
                   "favorites": _num(st.get("favoritesCount")), "views": _num(st.get("readingCount"))}
    except Exception as e:
        log.info("habr api %s: %s — читаю страницу", aid, e)
        await _from_page(s, a)
    if a.author_alias:
        try:
            u = await fetch(s, HABR_USER_API.format(alias=a.author_alias), want_json=True)
            a.author_stats = {"karma": _num(deep(u, "karma", "score")), "rating": _num(deep(u, "rating")),
                              "posts": _num(deep(u, "postCount", "postsCount", "publicationsCount")),
                              "followers": _num(deep(u, "followersCount", "followers"))}
        except Exception as e:
            log.info("habr user %s: %s", a.author_alias, e)
    return a


# ---------- vc.ru ----------

async def vc_article(s: aiohttp.ClientSession, item: Item) -> Article:
    a = Article(url=item.url, site="vc", title=item.title, author=item.author, published=item.published)
    cid = _id(item.url)
    try:
        j = await fetch(s, VC_API.format(id=cid), want_json=True)
        e = j.get("result") or j
        if isinstance(e, dict) and "entry" in e and isinstance(e["entry"], dict):
            e = e["entry"]
        a.via = "api"
        a.title = e.get("title") or a.title
        au = e.get("author") or {}
        a.author = au.get("name") or a.author
        a.author_url = au.get("url") or (f"https://vc.ru/id{au['id']}" if au.get("id") else "")
        a.author_id = str(au.get("id") or "")
        sub = e.get("subsite") or {}
        # тип подсайта: 1 — пользователь; 2 и др. — компания/сообщество. Пишет компания от своего имени → блог компании
        if au.get("type") not in (None, 1) or (sub.get("id") and sub.get("id") == au.get("id") and sub.get("type") not in (None, 1)):
            a.corporate = True
        elif au.get("type") == 1:
            a.corporate = False
        a.published = _num(e.get("date")) or a.published
        texts, imgs = [], 0
        for b in e.get("blocks") or []:
            t = b.get("type")
            d = b.get("data") or {}
            if t in ("text", "header", "quote", "list", "incut"):
                raw = json.dumps(d, ensure_ascii=False) if t == "list" else (d.get("text") or "")
                texts.append(html_to_text(raw))
                a.links += extract_links(raw)
            elif t in ("media", "image", "gallery"):
                imgs += max(1, len(d.get("items") or []))
        a.text, a.images = "\n\n".join(x for x in texts if x), imgs
        c = e.get("counters") or {}
        a.stats = {"score": _num(deep(e.get("likes") or {}, "counter", "summ")) or _num(c.get("likes")),
                   "comments": _num(c.get("comments")), "favorites": _num(c.get("favorites")),
                   "views": _num(c.get("views") or c.get("hits"))}
        a.author_stats = {"followers": _num(deep(au, "subscribersCount", "followersCount")),
                          "posts": _num(deep(au, "entriesCount", "entries"))}
        if not a.text:
            await _from_page(s, a)
    except Exception as ex:
        log.info("vc api %s: %s — читаю страницу", cid, ex)
        await _from_page(s, a)
    return a


# ---------- любая страница ----------

def _meta(h: str, name: str) -> str:
    m = re.search(rf'(?is)<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]*content=["\']([^"\']*)', h) or \
        re.search(rf'(?is)<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:name|property)=["\']{re.escape(name)}["\']', h)
    return htmllib.unescape(m.group(1)).strip() if m else ""


async def _from_page(s: aiohttp.ClientSession, a: Article):
    """Запасной путь: страница статьи — мета-теги, JSON-LD, основной текст."""
    h = await fetch(s, a.url)
    a.via = a.via or "page"
    a.title = a.title or _meta(h, "og:title")
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', h):
        try:
            ld = json.loads(block)
        except ValueError:
            continue
        au = deep(ld, "author")
        if isinstance(au, list) and au:
            au = au[0]
        if isinstance(au, dict):
            a.author = a.author or au.get("name") or ""
            a.author_url = a.author_url or au.get("url") or ""
            if au.get("@type") == "Organization" and a.corporate is None:
                a.corporate = True
        a.published = a.published or _ts(deep(ld, "datePublished") or "")
    a.author = a.author or _meta(h, "author") or _meta(h, "article:author")
    m = re.search(r"(?is)<article[^>]*>(.*?)</article>", h)
    body = m.group(1) if m else h
    a.text, a.images = html_to_text(body), count_images(body)
    a.links = a.links or extract_links(body)
    if re.search(r"(?i)tm-publication-label_variant-translation|>\s*Перевод\s*<", h):
        a.translation = True
    if "habr.com" in a.url and re.search(r"(?i)Блог компании", h):
        a.corporate = True


async def details(s: aiohttp.ClientSession, kind: str, item: Item) -> Article:
    if kind == "habr" or "habr.com" in item.url:
        return await habr_article(s, item)
    if kind == "vc" or "vc.ru" in item.url:
        return await vc_article(s, item)
    a = Article(url=item.url, site=urlparse(item.url).netloc, title=item.title, author=item.author,
                published=item.published)
    await _from_page(s, a)
    return a


async def read_feed(s: aiohttp.ClientSession, url: str) -> list[Item]:
    return parse_rss(await fetch(s, url))
