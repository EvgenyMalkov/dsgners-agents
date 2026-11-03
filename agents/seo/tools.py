"""Инструменты SEO-шника: аудит страниц и сайта, оформление статей, отчёты, ТЗ на правки."""
import json
import re

from core import common_tools as ct
from core.agent import Ctx
from core.seo import page, store

MAX_STEPS = 26      # статья: поиск конкурентов, чтение, ссылки, текст, SEO-пакет, обложка
MAX_TOKENS = 20000  # текст статьи целиком уходит одним save_document


def model_for(task: str) -> str | None:
    """Сильная модель (MODEL_SMART) — где качество окупается: решить, что писать (контент-план),
    написать текст статьи, сделать выводы из разбора. Остальное — обычная модель."""
    from core import config
    head = task[:600]
    if ("Составь контент-план" in head or "Статья по утверждённому плану" in head
            or "чему научиться" in head or "навык «Контент-план»" in head):
        return config.MODEL_SMART
    return None

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
    {
        "name": "submit_plan",
        "description": "Сдать ПЛАН статьи на согласование (этап 1 навыка «Написать статью»). После «Ок» владельца "
                       "система сама запустит этап 2 — написание по этому плану. После submit_plan вызови finish.",
        "input_schema": {"type": "object", "properties": {
            "title": {"type": "string", "description": "рабочий заголовок статьи"},
            "main_query": {"type": "string"},
            "plan": {"type": "string", "description": "весь план в Markdown: запросы, конкуренты, преимущество, H2/H3, объём"},
        }, "required": ["title", "main_query", "plan"]},
    },
    {
        "name": "set_seo_fields",
        "description": "Итоговые SEO-поля статьи в машинном виде — после «Ок» владельца система сама запишет их на сайт "
                       "(текст статьи не меняется). Вызывай в дополнение к документу «seo», когда оформляешь существующую статью.",
        "input_schema": {"type": "object", "properties": {
            "url": {"type": "string"},
            "seo_title": {"type": "string"}, "meta_description": {"type": "string"},
            "og_title": {"type": "string"}, "og_description": {"type": "string"},
            "jsonld": {"type": "string", "description": "JSON-LD целиком (без тега script), проверенный validate_jsonld"},
            "images": {"type": "array", "items": {"type": "object", "properties": {
                "src": {"type": "string"}, "alt": {"type": "string"}}, "required": ["src", "alt"]},
                "description": "alt для картинок: адрес картинки как на странице → alt"},
        }, "required": ["url"]},
    },
    {
        "name": "set_author_letter",
        "description": "Письмо автору статьи в машинном виде — после «Ок» владельца система отправит его от редакции: "
                       "на почту автора + уведомление на сайте. Только для статей авторов.",
        "input_schema": {"type": "object", "properties": {
            "url": {"type": "string"},
            "subject": {"type": "string", "description": "тема письма, до 80 знаков, по делу: «Как вернуть читателей статье „…“»"},
            "text": {"type": "string", "description": "текст письма, до 1200 знаков"},
        }, "required": ["url", "subject", "text"]},
    },
    {
        "name": "keyword_data",
        "description": "Данные о спросе по теме: фразы из выгрузок Вордстата с частотами, запросы из Вебмастера "
                       "(наши позиции, показы, клики) и наши страницы по теме.",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
    },
    {
        "name": "propose_topics",
        "description": "Сдать контент-план: список тем на согласование. После «Ок» владельца по каждой теме "
                       "автоматически запустится написание плана статьи. После propose_topics — save_document и finish.",
        "input_schema": {"type": "object", "properties": {"topics": {"type": "array", "items": {
            "type": "object", "properties": {
                "kind": {"type": "string", "enum": ["new", "update", "author"],
                         "description": "new — новая статья редакции; update — обновить статью РЕДАКЦИИ; "
                                        "author — статья автора: SEO-поля + рекомендации автору"},
                "title": {"type": "string"},
                "main_query": {"type": "string"},
                "url": {"type": "string", "description": "адрес статьи — для update и author"},
                "why": {"type": "string", "description": "почему, с цифрами из данных"},
                "angle": {"type": "string", "description": "чем будет лучше топа"},
            }, "required": ["kind", "title", "main_query", "why", "angle"]}}}, "required": ["topics"]},
    },
    {
        "name": "propose_rules",
        "description": "Предложить новые правила для себя по итогам разбора результатов. После «Ок» владельца "
                       "правила автоматически добавятся в твои правила. Только подтверждённые данными выводы.",
        "input_schema": {"type": "object", "properties": {
            "rules": {"type": "array", "items": {"type": "string"},
                      "description": "3–7 правил, каждое — конкретное указание, как писать/оформлять, с опорой на цифры"},
        }, "required": ["rules"]},
    },
    ct.READ_PAGE,
    ct.SAVE_DOCUMENT,
    ct.ask_colleague_tool({
        "webdesigner": "веб-дизайнер: макет блока/страницы и ТЗ для разработки",
        "designer": "графический дизайнер: обложки и картинки для статей",
    }),
    ct.FINISH,
]

# поиск в интернете на стороне Anthropic (~1 цент за поиск) — для анализа конкурентов и проверки фактов.
# Должен быть включён в console.anthropic.com (настройки организации); выключить: WEB_SEARCH=0 в .env
from core import config as _cfg  # noqa: E402
if _cfg.WEB_SEARCH:
    TOOLS.insert(len(TOOLS) - 4, {"type": "web_search_20250305", "name": "web_search", "max_uses": 6})


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


async def submit_plan(inp: dict, ctx: Ctx):
    (ctx.job_dir / "plan.md").write_text(f"# План: {inp['title']}\nГлавный запрос: {inp['main_query']}\n\n{inp['plan']}", "utf-8")
    ctx.meta.update(stage="plan", title=inp["title"], main_query=inp["main_query"])
    if ctx.progress:
        await ctx.progress("план статьи готов")
    return [{"type": "text", "text": "План сохранён. Теперь вызови finish — владелец согласует план."}]


async def propose_rules(inp: dict, ctx: Ctx):
    rules = [r.strip() for r in inp.get("rules", []) if r.strip()][:10]
    ctx.meta["rules"] = rules
    (ctx.job_dir / "rules.md").write_text("# Предлагаемые правила\n\n" + "\n".join(f"- {r}" for r in rules), "utf-8")
    return [{"type": "text", "text": f"Предложено правил: {len(rules)}. После «Ок» владельца они вступят в силу."}]


async def keyword_data(inp: dict, ctx: Ctx):
    from core.seo import keywords
    topic = inp["topic"]
    out = []
    ws = keywords.search(topic)
    out.append("Вордстат: " + ("\n" + "\n".join(f"  «{k['phrase']}» — {k['freq']}/мес" for k in ws[:30])
                               if ws else ("по теме ничего" if keywords.count() else "выгрузок нет")))
    t = keywords.stems(topic)
    qs = [q for q in (store.get("wm_queries") or []) if len(t & keywords.stems(q["query"])) >= min(2, len(t))]
    qs.sort(key=lambda q: -q["shows"])
    out.append("Вебмастер (4 недели): " + ("\n" + "\n".join(
        f"  «{q['query']}» — поз. {q['pos']:.1f}, показов {q['shows']}, кликов {q['clicks']}" for q in qs[:25])
        if qs else "по теме запросов нет или Вебмастер не подключён"))
    rel = await related_pages({"topic": topic}, ctx)
    out.append("Наши страницы по теме:\n" + rel[0]["text"])
    return [{"type": "text", "text": "\n\n".join(out)}]


async def propose_topics(inp: dict, ctx: Ctx):
    from core.seo import monitor
    topics = [t for t in inp.get("topics", []) if t.get("title")][:10]
    for t in topics:   # обновлять можно только статьи редакции — чужую статью переводим в «рекомендации автору»
        if t.get("kind") == "update" and t.get("url") and not monitor.is_own(t["url"]):
            t["kind"] = "author"
    ctx.meta["topics"] = topics
    md = ["# Контент-план", ""]
    for i, t in enumerate(topics, 1):
        kind = {"update": "Обновить", "author": "Автору"}.get(t.get("kind"), "Новая")
        md += [f"## {i}. {kind}: {t['title']}", f"Запрос: {t.get('main_query', '')}" + (f" · {t['url']}" if t.get("url") else ""),
               f"Почему: {t.get('why', '')}", f"Угол: {t.get('angle', '')}", ""]
    (ctx.job_dir / "topics.md").write_text("\n".join(md), "utf-8")
    if ctx.progress:
        await ctx.progress(f"контент-план: {len(topics)} тем")
    return [{"type": "text", "text": f"Тем: {len(topics)}. Теперь save_document «content-plan» с выводами и finish."}]


async def set_seo_fields(inp: dict, ctx: Ctx):
    f = {k: v for k, v in inp.items() if v}
    if f.get("jsonld"):
        chk = await validate_jsonld({"jsonld": f["jsonld"]}, ctx)
        if not chk[0]["text"].startswith("Разметка в порядке"):
            return [{"type": "text", "text": "Не сохранил: " + chk[0]["text"]}]
    for k, lim in (("seo_title", 75), ("meta_description", 180)):
        if len(f.get(k, "")) > lim:
            return [{"type": "text", "text": f"{k} длиннее {lim} знаков — сократи."}]
    ctx.meta["seo_fields"] = f
    return [{"type": "text", "text": "SEO-поля сохранены — после «Ок» уйдут на сайт."}]


async def set_author_letter(inp: dict, ctx: Ctx):
    from core.seo import monitor
    if monitor.is_own(inp["url"]):
        return [{"type": "text", "text": "Это статья редакции — письмо автору не нужно."}]
    if len(inp["text"]) > 1500:
        return [{"type": "text", "text": f"Письмо {len(inp['text'])} знаков — сократи до 1200."}]
    ctx.meta["letter"] = {"url": inp["url"], "subject": inp["subject"].strip()[:100], "text": inp["text"].strip()}
    return [{"type": "text", "text": "Письмо сохранено — после «Ок» уйдёт автору."}]


HANDLERS = {"set_seo_fields": set_seo_fields, "set_author_letter": set_author_letter, "submit_plan": submit_plan, "keyword_data": keyword_data, "propose_topics": propose_topics, "propose_rules": propose_rules,"check_site": check_site, "check_page": check_page, "related_pages": related_pages,
            "validate_jsonld": validate_jsonld}


async def handle(name: str, inp: dict, ctx: Ctx):
    if name in HANDLERS:
        return await HANDLERS[name](inp, ctx)
    if name in ct.COMMON_HANDLERS:
        return await ct.COMMON_HANDLERS[name](inp, ctx)
    return [{"type": "text", "text": f"Нет инструмента {name}"}]
