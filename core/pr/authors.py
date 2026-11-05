"""Есть ли автор на дизайнерс и приносил ли он эту статью (ТЗ, раздел 4).

Список авторов берём из базы SEO-мониторинга (data/seo.db: адреса статей /<автор>/<номер>-…),
профили читаем раз в неделю (имя и внешние ссылки) — PR_DSG_PROFILES_PER_RUN за раз, со скоростью SEO_CRAWL_RPS.

Совпадение:
  sure  — в профиле на дизайнерс есть ссылка на тот же профиль/канал/сайт, что у автора на площадке;
  maybe — совпало только имя (или ник = адрес профиля) — решает владелец;
  none  — на дизайнерс нет.
"""
import asyncio
import difflib
import html as htmllib
import logging
import re
import time
from urllib.parse import urlparse

from core import config
from core.pr import sources, store

log = logging.getLogger("pr.authors")
PER_RUN = int(config.env("PR_DSG_PROFILES_PER_RUN", "400"))
SITE = f"https://{config.SEO_SITE}"


def _dsg_posts() -> dict[str, list[str]]:
    """{slug: [заголовки статей]} из базы SEO-мониторинга."""
    try:
        from core.seo import monitor, store as seo_store
        out: dict[str, list[str]] = {}
        for r in seo_store.db().execute("SELECT url, title, h1 FROM urls WHERE in_sitemap=1"):
            if not monitor.is_content(r["url"]):
                continue
            out.setdefault(monitor.author_of(r["url"]), []).append(r["h1"] or r["title"] or "")
        out.pop("", None)
        return out
    except Exception:
        log.exception("seo db")
        return {}


def norm_link(u: str) -> str:
    """Ссылка → ключ для сравнения: t.me/name, habr.com/users/alias, vc.ru/id5, домен сайта."""
    u = (u or "").strip().lower()
    if u.startswith("@"):
        return "t.me/" + u[1:]
    u = re.sub(r"^https?://(www\.)?", "", u).split("?")[0].split("#")[0].rstrip("/")
    u = u.replace("telegram.me/", "t.me/").replace("ru/users/", "users/")
    m = re.match(r"(t\.me|vk\.com|behance\.net|dribbble\.com|habr\.com/users|vc\.ru)/([^/]+)", u)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return u.split("/")[0] if "." in u else u


def _name(n: str) -> str:
    return re.sub(r"[^a-zа-яё ]", "", (n or "").lower().replace("ё", "е")).strip()


def _links_of(h: str) -> list[str]:
    links = set()
    for href in re.findall(r'(?i)href=["\']([^"\']+)["\']', h):
        href = htmllib.unescape(href)
        host = urlparse(href).netloc.lower()
        if href.startswith("http") and config.SEO_SITE not in host:
            links.add(href)
    return sorted(links)[:40]


async def refresh(limit: int = PER_RUN) -> str:
    """Перечитать самые старые профили авторов дизайнерс."""
    slugs = list(_dsg_posts())
    checked = store.dsg_checked()
    week = time.time() - 7 * 86400
    todo = sorted([s for s in slugs if checked.get(s, 0) < week], key=lambda s: checked.get(s, 0))[:limit]
    if not todo:
        return f"профили свежие ({len(slugs)})"
    delay = 1 / max(0.2, config.SEO_CRAWL_RPS)
    n = 0
    async with sources.session() as s:
        for slug in todo:
            try:
                h = await sources.fetch(s, f"{SITE}/{slug}")
                name = sources._meta(h, "og:title") or (re.search(r"(?is)<h1[^>]*>(.*?)</h1>", h) or [None, ""])[1]
                name = sources.html_to_text(name).split("—")[0].split("|")[0].strip()
                store.save_dsg_author(slug, name, _links_of(h))
                n += 1
            except Exception as e:
                store.save_dsg_author(slug, "", [])
                log.info("dsg profile %s: %s", slug, e)
            await asyncio.sleep(delay)
    return f"профилей перечитано {n} из {len(todo)}, всего авторов {len(slugs)}"


def match(a: dict, ext_links: list[str]) -> tuple[str, str]:
    """(sure|maybe|none, slug) для статьи из pr.db. ext_links — профиль автора и его контакты на площадке."""
    keys = {norm_link(u) for u in ext_links if u}
    keys.discard("")
    alias = (urlparse(a.get("author_url") or "").path.rstrip("/").split("/") or [""])[-1].lower()
    name = _name(a.get("author"))
    maybe = ""
    for d in store.dsg_authors():
        dkeys = {norm_link(u) for u in d["links"]}
        if keys & dkeys:
            return "sure", d["slug"]
        if not maybe and ((name and len(name) > 5 and _name(d["name"]) == name) or (alias and len(alias) > 3 and alias == d["slug"])):
            maybe = d["slug"]
    return ("maybe", maybe) if maybe else ("none", "")


def has_article(slug: str, title: str) -> bool:
    """Эта статья уже есть у автора на дизайнерс (похожий заголовок)."""
    t = _name(title)
    for other in _dsg_posts().get(slug, []):
        if t and difflib.SequenceMatcher(None, t, _name(other)).ratio() > 0.8:
            return True
    return False
