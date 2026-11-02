"""Инструменты SEO-шника: аудит страниц и сайта, оформление статей, отчёты, ТЗ на правки."""
import json
import re

from core import common_tools as ct
from core.agent import Ctx
from core.seo import page, store

TOOLS = [
    {
        "name": "check_site",
        "description": "Техническая проверка сайта: доступность, редирект на https, robots.txt, sitemap.xml "
                       "(сколько URL, примеры), время ответа главной.",
        "input_schema": {"type": "object", "properties": {"site": {"type": "string", "description": "домен, напр. dsgners.ru"}},
                         "required": ["site"]},
    },
    {
        "name": "check_page",
        "description": "Автопроверка SEO одной страницы: title, description, H1/H2, canonical, noindex, OpenGraph, "
                       "разметка schema.org (какая уже есть), alt у картинок (с адресами картинок без alt), "
                       "внутренние ссылки, редиректы, скорость — и список проблем по важности.",
        "input_schema": {"type": "object", "properties": {
            "url": {"type": "string"},
            "is_article": {"type": "boolean", "description": "статья/новость/список — проверять строже (разметка Article, объём)"},
        }, "required": ["url"]},
    },
    {
        "name": "related_pages",
        "description": "Найти на dsgners.ru страницы по теме (по заголовкам из sitemap, которые знает мониторинг) — "
                       "для внутренних ссылок из статьи и в статью.",
        "input_schema": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "тема или ключевые слова, например «логотип кофейня брендинг»"},
            "exclude_url": {"type": "string"},
        }, "required": ["topic"]},
    },
    {
        "name": "validate_jsonld",
        "description": "Проверить JSON-LD разметку перед сдачей: парсится ли, есть ли обязательные поля "
                       "(Article: headline, image, datePublished, author; BreadcrumbList: itemListElement).",
        "input_schema": {"type": "object", "properties": {"jsonld": {"type": "string"}}, "required": ["jsonld"]},
    },
    ct.READ_PAGE,
    ct.SAVE_DOCUMENT,
    ct.ask_colleague_tool({
        "webdesigner": "веб-дизайнер: макет блока/страницы и ТЗ для разработки",
        "designer": "графический дизайнер: обложки и картинки для статей",
    }),
    ct.FINISH,
]


def system_extra() -> str:
    from core import config
    from core.seo import apis
    return (f"# Данные, которые у тебя есть\n"
            f"- Мониторинг сайта {config.SEO_SITE} знает {len(store.known_urls())} адресов из sitemap.\n"
            f"- Яндекс Вебмастер и Метрика: {'подключены (данные приходят в отчётах мониторинга)' if apis.has_yandex() else 'не подключены — частотности, позиции и трафик не выдумывай'}.")


async def check_site(inp: dict, ctx: Ctx):
    import time
    site = re.sub(r"^https?://", "", inp["site"]).strip("/")
    if ctx.progress:
        await ctx.progress(f"проверяю {site}")
    out = []
    t = time.monotonic()
    try:
        st, final, html = await ct.fetch(f"https://{site}/")
        out.append(f"Главная: HTTP {st}, итоговый адрес {final}, {time.monotonic() - t:.1f} с, {len(html) // 1024} КБ HTML")
    except Exception as e:
        out.append(f"Главная недоступна: {e}")
    try:
        st, final, _ = await ct.fetch(f"http://{site}/", 200_000)
        out.append(f"http:// → {final} ({'есть редирект на https' if final.startswith('https') else 'НЕТ редиректа на https'})")
    except Exception as e:
        out.append(f"http:// не проверен: {e}")
    sitemaps = []
    try:
        st, _, robots = await ct.fetch(f"https://{site}/robots.txt", 200_000)
        out.append(f"robots.txt: HTTP {st}\n```\n{robots[:2500]}\n```")
        sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
    except Exception as e:
        out.append(f"robots.txt: ошибка {e}")
    for sm in (sitemaps or [f"https://{site}/sitemap.xml"])[:3]:
        try:
            st, _, xml = await ct.fetch(sm, 5_000_000)
            locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml)
            kind = "индекс sitemap-ов" if "<sitemapindex" in xml else "sitemap"
            out.append(f"{sm}: HTTP {st}, {kind}, адресов: {len(locs)}. Примеры: " + ", ".join(locs[:8]))
        except Exception as e:
            out.append(f"{sm}: ошибка {e}")
    return [{"type": "text", "text": "\n".join(out)}]


async def check_page(inp: dict, ctx: Ctx):
    url = inp["url"]
    if ctx.progress:
        await ctx.progress(f"проверяю страницу {url[:60]}")
    rep = await page.check_url(url, is_article=inp.get("is_article", True))
    try:
        store.save_check(rep)
    except Exception:
        pass
    txt = rep.summary()
    if rep.images_no_alt:
        txt += "\nКартинки без alt:\n" + "\n".join(f"  {u}" for u in rep.images_no_alt[:30])
    return [{"type": "text", "text": txt}]


def _stems(s: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-zа-яё0-9]+", s.lower()) if len(w) > 3}


async def related_pages(inp: dict, ctx: Ctx):
    topic = _stems(inp["topic"])
    excl = page.norm(inp["exclude_url"]) if inp.get("exclude_url") else ""
    rows = store.pages_with_titles()
    if not rows:
        return [{"type": "text", "text": "Мониторинг ещё не обошёл сайт — заголовков страниц нет. "
                                         "Подбери ссылки по sitemap (check_site) или пропусти этот пункт."}]
    scored = []
    for r in rows:
        if r["url"] == excl:
            continue
        hit = len(topic & _stems(f"{r['title']} {r['h1']} {r['url'].replace('-', ' ')}"))
        if hit:
            scored.append((hit, r))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return [{"type": "text", "text": "По этой теме страниц не нашлось — попробуй другие слова."}]
    return [{"type": "text", "text": "\n".join(f"- {r['title'] or r['h1']} — {r['url']}" for _, r in scored[:15])}]


REQUIRED = {
    "Article": ["headline", "image", "datePublished", "author"],
    "NewsArticle": ["headline", "image", "datePublished", "author"],
    "BlogPosting": ["headline", "image", "datePublished", "author"],
    "BreadcrumbList": ["itemListElement"],
    "ItemList": ["itemListElement"],
    "CollectionPage": ["name"],
    "FAQPage": ["mainEntity"],
    "HowTo": ["name", "step"],
}


async def validate_jsonld(inp: dict, ctx: Ctx):
    raw = re.sub(r"(?is)^\s*<script[^>]*>|</script>\s*$", "", inp["jsonld"].strip()).strip()
    try:
        data = json.loads(raw)
    except Exception as e:
        return [{"type": "text", "text": f"JSON не парсится: {e}. Исправь и проверь снова."}]
    items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
    probs = []
    if isinstance(data, dict) and "@context" not in data:
        probs.append("нет @context (нужно \"https://schema.org\")")
    for it in items:
        t = it.get("@type") if isinstance(it, dict) else None
        t = t[0] if isinstance(t, list) else t
        for f in REQUIRED.get(t or "", []):
            if not it.get(f):
                probs.append(f"{t}: нет поля {f}")
        if t in ("Article", "NewsArticle", "BlogPosting"):
            if len(str(it.get("headline", ""))) > 110:
                probs.append("headline длиннее 110 символов")
            if not re.match(r"\d{4}-\d{2}-\d{2}", str(it.get("datePublished", ""))):
                probs.append("datePublished не в формате ISO 8601 (2026-11-02 или 2026-11-02T10:00:00+03:00)")
    types = [i.get("@type") for i in items if isinstance(i, dict)]
    if probs:
        return [{"type": "text", "text": "Проблемы: " + "; ".join(probs)}]
    return [{"type": "text", "text": f"Разметка в порядке. Типы: {types}"}]


HANDLERS = {"check_site": check_site, "check_page": check_page, "related_pages": related_pages,
            "validate_jsonld": validate_jsonld}


async def handle(name: str, inp: dict, ctx: Ctx):
    if name in HANDLERS:
        return await HANDLERS[name](inp, ctx)
    if name in ct.COMMON_HANDLERS:
        return await ct.COMMON_HANDLERS[name](inp, ctx)
    return [{"type": "text", "text": f"Нет инструмента {name}"}]
