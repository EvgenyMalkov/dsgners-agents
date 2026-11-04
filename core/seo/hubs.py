"""Автообновление хабов /education/<направление>: агент собирает hubs.json, сайт забирает его сам.

Что делает (ТЗ — Claude Doc «ТЗ: автообновление хабов (hubs.json)»):
- quick()   — каждые 30 мин: новые посты → раскладка по подтемам → в начало ленты «Свежее»; удалённые и закрытые
              статьи убираются из блоков. Публикуется сама (если ничего подозрительного).
- weekly()  — понедельник 10:00: пересборка подборок всех подстраниц (pick.build) с правилами стабильности
              (≤2 замены на блок, якоря с трафиком не трогаем, блок < 3 статей скрыт) → черновик на «Ок»
              (HUBS_AUTO_APPLY=1 — публикуется сразу, присылается только отчёт).
- daily()   — 07:00: страницы хабов на сайте отвечают 200 и показывают свежую hubs-version, статьи из блоков живы.
- monthly() — 1-е число: данные по структуре (/cluster) с кандидатами в новые блоки и подстраницы — на «Ок».
- serve     — GET /hubs.json на HUBS_PORT с токеном HUBS_TOKEN (Bearer), ETag = номер версии.
Версии хранятся в data/seo.db (hub_versions); откат — /hubs_rollback N (публикует старое как новую версию).
"""
import datetime as dt
import json
import logging
import math
import re
import time

import anthropic
from aiohttp import web

from core import config, spend
from core.seo import inventory, page, pick, store

log = logging.getLogger("seo.hubs")
SCHEMA = 1
SHOW = 9            # статей в блоке
MIN_BLOCK = 3       # меньше уверенных статей — блок скрыт
MAX_SWAPS = 2       # замен на блок за неделю
PER_PAGE = 20       # карточек в порции ленты
_ask = None         # async (text, path) — черновик в Telegram с кнопками «Ок» / «Не применять»


def setup(ask):
    global _ask
    _ask = ask


def _db():
    d = store.db()
    d.execute("CREATE TABLE IF NOT EXISTS hub_versions(version INTEGER PRIMARY KEY, ts REAL, note TEXT, content TEXT)")
    return d


def current() -> dict | None:
    r = _db().execute("SELECT content FROM hub_versions ORDER BY version DESC LIMIT 1").fetchone()
    return json.loads(r[0]) if r else None


def versions(n: int = 10) -> list[tuple]:
    return list(_db().execute("SELECT version, ts, note FROM hub_versions ORDER BY version DESC LIMIT ?", (n,)))


def version_doc(v: int) -> dict | None:
    r = _db().execute("SELECT content FROM hub_versions WHERE version=?", (v,)).fetchone()
    return json.loads(r[0]) if r else None


# ---------- данные ----------

_TR = dict(zip("абвгдеёзийклмнопрстуфхцы", "abvgdeezijklmnoprstufhcy")) | {
    "ж": "zh", "ч": "ch", "ш": "sh", "щ": "sch", "ю": "yu", "я": "ya", "ь": "", "ъ": "", "э": "e"}


def slug(text: str) -> str:
    s = "".join(_TR.get(ch, ch) for ch in text.lower())
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40]


def post_id(url: str) -> int | None:
    m = re.search(r"/(\d+)-[^/]*/?$", url)
    return int(m.group(1)) if m else None


def _alive() -> set[str]:
    """Посты, которые есть в sitemap и при последней проверке не были битыми или закрытыми от индекса."""
    return {r[0] for r in store.db().execute(
        "SELECT url FROM urls WHERE in_sitemap=1 AND (status IS NULL OR status=200) "
        "AND (issues IS NULL OR issues NOT LIKE '%\"noindex\"%')")}


def _hub_key(direction: str) -> str | None:
    return next((k for k, (d, _, path, _) in pick.SUBPAGES.items() if d == direction and path.count("/") == 2), None)


def _feeds(alive: set[str]) -> dict[str, list[int]]:
    """Лента «Свежее» для каждой страницы: все живые посты подтемы (у хаба — всего направления), новые сверху."""
    posts = inventory._posts()
    try:
        from core.seo import classify
        llm = classify.results()
    except Exception:
        llm = {}
    by_sub: dict[tuple, set] = {}
    for direction, subs in inventory.T.items():
        for sub, (_, pat) in subs.items():
            rx, lab = re.compile(pat), f"{direction} / {sub}"
            by_sub[(direction, sub)] = {p["url"] for p in posts if p["url"] in alive
                                        and (rx.search(p["text"]) or lab in llm.get(p["url"], ()))}
    out = {}
    for key, (direction, sub, path, _) in pick.SUBPAGES.items():
        if key == _hub_key(direction):
            urls = set().union(*(v for (d, _), v in by_sub.items() if d == direction))
        else:
            urls = by_sub.get((direction, sub), set())
        ids = {post_id(u) for u in urls} - {None}
        out[key] = sorted(ids, reverse=True)
    return out


TEXT_TOOL = {
    "name": "texts",
    "description": "Тексты страницы хаба.",
    "input_schema": {"type": "object", "properties": {
        "title": {"type": "string", "description": "SEO title до 60 знаков, без названия сайта; главный запрос в начале"},
        "meta_description": {"type": "string", "description": "120–160 знаков, что человек найдёт на странице"},
        "h1": {"type": "string", "description": "короткий H1, 1–3 слова"},
        "intro": {"type": "string", "description": "1–2 предложения под H1"},
        "subtitles": {"type": "array", "items": {"type": "string"},
                      "description": "подводка 1–2 предложения для каждого блока, по порядку блоков"},
        "faq": {"type": "array", "items": {"type": "object", "properties": {
            "q": {"type": "string"}, "a": {"type": "string"}}, "required": ["q", "a"]},
            "description": "3–5 вопросов, которые реально ищут, и короткие ответы по 2–3 предложения"},
    }, "required": ["title", "meta_description", "h1", "intro", "subtitles", "faq"]},
}


async def _texts(key: str, force: bool = False) -> dict:
    """Тексты страницы пишутся один раз (и заново — только по месячному пересмотру), хранятся в kv hubs_texts:<key>."""
    saved = store.get(f"hubs_texts:{key}")
    direction, sub, path, blocks = pick.SUBPAGES[key]
    if saved and not force and len(saved.get("subtitles", [])) == len(blocks):
        return saved
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = await client.messages.create(
        model=config.MODEL, max_tokens=2500, tools=[TEXT_TOOL], tool_choice={"type": "tool", "name": "texts"},
        system="Ты SEO-редактор дизайн-платформы дизайнерс (dsgners.ru). Пишешь по-русски, просто и конкретно, "
               "без воды и превосходных степеней. Страница — подборки статей сообщества по теме, не услуги.",
        messages=[{"role": "user", "content": f"Страница {path}, тема «{sub}» (направление «{direction}»). Блоки по порядку:\n"
                   + "\n".join(f"{j + 1}. {h} — {d}" for j, (h, d) in enumerate(blocks))}])
    spend.record("seo", resp.usage, config.MODEL)
    out = next((b.input for b in resp.content if b.type == "tool_use"), {}) or {}
    subs = [str(x) for x in (out.get("subtitles") or [])][:len(blocks)]
    subs += [""] * (len(blocks) - len(subs))
    t = {"title": str(out.get("title", sub)), "meta_description": str(out.get("meta_description", "")),
         "h1": str(out.get("h1", sub)), "intro": str(out.get("intro", "")), "subtitles": subs,
         "faq": [f for f in (out.get("faq") or []) if isinstance(f, dict) and f.get("q") and f.get("a")][:5]}
    store.put(f"hubs_texts:{key}", t)
    return t


def _pick_block(cand: list[dict], prev: list[str], alive: set[str]) -> tuple[list[str], list[str], list[str]]:
    """Новый состав блока с правилами стабильности → (состав, вошли, вышли)."""
    cand = [r for r in cand if r["url"] in alive]
    info = {r["url"]: r for r in cand}
    prev_alive = [u for u in prev if u in alive]
    desired = [r["url"] for r in cand[:SHOW]]
    if not prev:
        return desired, desired, []
    keep = list(prev_alive)
    adds = [u for u in desired if u not in keep]
    added = []
    while adds and len(keep) < SHOW:
        u = adds.pop(0)
        keep.append(u)
        added.append(u)
    # кандидаты на выход: не в желаемом составе и без визитов из поиска (якоря с трафиком не трогаем)
    outs = sorted((u for u in keep if u not in desired and info.get(u, {}).get("visits", 0) == 0),
                  key=lambda u: info.get(u, {}).get("rank", -1))
    swaps = 0
    while adds and outs and swaps < MAX_SWAPS:
        o, u = outs.pop(0), adds.pop(0)
        keep[keep.index(o)] = u
        added.append(u)
        swaps += 1
    keep.sort(key=lambda u: -info.get(u, {}).get("rank", -1))
    removed = [u for u in prev if u not in keep]
    return keep, added, removed


async def _build(prev: dict | None, repick: bool) -> dict:
    """Собрать документ. repick=False — только лента и уборка мёртвых статей; True — новые подборки из pick:<key>."""
    alive = _alive()
    feeds = _feeds(alive)
    prev_pages = {p["path"]: p for p in (prev or {}).get("pages", [])}
    titles = {r["url"]: (r["title"] or "") for r in store.db().execute("SELECT url, title FROM urls")}
    pages = []
    for key, (direction, sub, path, blocks) in pick.SUBPAGES.items():
        hub = _hub_key(direction)
        hub_path = pick.SUBPAGES[hub][2] if hub else None
        pp = prev_pages.get(path)
        texts = await _texts(key) if (repick or not pp) else None
        pk = (store.get(f"pick:{key}") or {}) if repick else {}
        out_blocks = []
        prev_blocks = {b["id"]: b for b in (pp or {}).get("blocks", [])}
        for j, (h2, _) in enumerate(blocks):
            bid = slug(h2)
            pb = prev_blocks.get(bid, {})
            prev_urls = [p["url"] for p in pb.get("posts", [])]
            if repick:
                urls, _, _ = _pick_block(pk.get("ranked", {}).get(h2, []), prev_urls, alive)
            else:
                urls = [u for u in prev_urls if u in alive]
            sub_t = texts["subtitles"][j] if texts else pb.get("subtitle", "")
            out_blocks.append({"id": bid, "h2": h2, "subtitle": sub_t, "hidden": len(urls) < MIN_BLOCK,
                               "posts": [{"id": post_id(u), "url": u, "title": titles.get(u, "")} for u in urls if post_id(u)]})
        src = texts or pp or {}
        h1 = (texts or {}).get("h1") or (pp or {}).get("h1") or sub
        pages.append({
            "key": key, "path": path, "parent": None if key == hub else hub_path,
            "type": "hub" if key == hub else "subpage",
            "status": (pp or {}).get("status", "live"), "redirect_to": (pp or {}).get("redirect_to"),
            "title": src.get("title", sub), "meta_description": src.get("meta_description", ""),
            "h1": h1, "intro": src.get("intro", ""), "nav": [],
            "blocks": out_blocks,
            "feed": {"h2": f"Свежее: {h1.lower()}", "per_page": PER_PAGE, "post_ids": feeds.get(key, [])},
            "faq": src.get("faq", []),
        })
    # навигация: у хаба — его подстраницы, у подстраницы — хаб и соседи
    live = [p for p in pages if p["status"] == "live"]
    for p in pages:
        hub_path = p["path"] if p["type"] == "hub" else p["parent"]
        p["nav"] = [{"title": q["h1"], "path": q["path"]} for q in live
                    if q["path"] != p["path"] and (q["path"] == hub_path or q["parent"] == hub_path)]
    return {"schema": SCHEMA, "version": (prev or {}).get("version", 0),
            "generated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).isoformat(timespec="seconds"),
            "pages": pages}


def _guard(old: dict | None, new: dict) -> list[str]:
    """Что выглядит как поломка, а не как обычное обновление."""
    if not old:
        return []
    bad = []
    newp = {p["path"]: p for p in new["pages"]}
    for p in old["pages"]:
        if p["status"] != "live":
            continue
        q = newp.get(p["path"])
        if not q:
            bad.append(f"пропала страница {p['path']}")
            continue
        nb = {b["id"]: b for b in q["blocks"]}
        for b in p["blocks"]:
            n_old, n_new = len(b["posts"]), len(nb.get(b["id"], {}).get("posts", []))
            if n_old >= 4 and n_new < n_old / 2:
                bad.append(f"{p['path']} · «{b['h2']}»: было {n_old} статей, стало {n_new}")
        f_old, f_new = len(p["feed"]["post_ids"]), len(q["feed"]["post_ids"])
        if f_old >= 10 and f_new < f_old * 0.8:
            bad.append(f"{p['path']}: лента сократилась с {f_old} до {f_new}")
    return bad


def _same(a: dict | None, b: dict) -> bool:
    strip = lambda d: json.dumps(d.get("pages", []) if d else None, ensure_ascii=False, sort_keys=True)
    return a is not None and strip(a) == strip(b)


def publish(doc: dict, note: str, force: bool = False) -> tuple[bool, list[str]]:
    cur = current()
    bad = [] if force else _guard(cur, doc)
    if bad:
        return False, bad
    doc = dict(doc, version=(cur or {}).get("version", 0) + 1)
    d = _db()
    d.execute("INSERT INTO hub_versions(version, ts, note, content) VALUES(?,?,?,?)",
              (doc["version"], time.time(), note, json.dumps(doc, ensure_ascii=False)))
    d.commit()
    out = config.DATA / "hubs" / "hubs.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_public(doc), ensure_ascii=False, indent=1), "utf-8")
    return True, []


def _public(doc: dict) -> dict:
    """Что отдаём сайту: без служебных полей (ключ страницы, заголовки статей — они только для отчётов)."""
    pages = []
    for p in doc["pages"]:
        q = {k: v for k, v in p.items() if k != "key"}
        q["blocks"] = [dict(b, posts=[{"id": x["id"], "url": x["url"]} for x in b["posts"]]) for b in p["blocks"]]
        pages.append(q)
    return dict(doc, pages=pages)


def diff_text(old: dict | None, new: dict) -> str:
    site = f"https://{config.SEO_SITE}"
    if not old:
        lines = ["Первая версия хабов:"]
        for p in new["pages"]:
            shown = [b for b in p["blocks"] if not b["hidden"]]
            lines.append(f"• {p['path']} — блоков {len(shown)} из {len(p['blocks'])}, в ленте {len(p['feed']['post_ids'])}")
        return "\n".join(lines)
    oldp = {p["path"]: p for p in old["pages"]}
    lines = []
    for p in new["pages"]:
        o = oldp.get(p["path"])
        if not o:
            lines.append(f"➕ новая страница {p['path']}")
            continue
        ob = {b["id"]: b for b in o["blocks"]}
        part = []
        for b in p["blocks"]:
            was = {x["url"]: x for x in ob.get(b["id"], {}).get("posts", [])}
            now = {x["url"]: x for x in b["posts"]}
            ins = [now[u] for u in now if u not in was]
            outs = [was[u] for u in was if u not in now]
            flag = ""
            if b["hidden"] != ob.get(b["id"], {}).get("hidden", b["hidden"]):
                flag = " — блок скрыт (меньше 3 статей)" if b["hidden"] else " — блок снова показан"
            if ins or outs or flag:
                part.append(f"  «{b['h2']}»{flag}")
                part += [f"    + {x['title'] or x['url'].replace(site, '')}" for x in ins]
                part += [f"    − {x['title'] or x['url'].replace(site, '')}" for x in outs]
        d_feed = len(p["feed"]["post_ids"]) - len(o["feed"]["post_ids"])
        if d_feed:
            part.append(f"  лента: {'+' if d_feed > 0 else ''}{d_feed}")
        if part:
            lines += [p["path"]] + part
    return "\n".join(lines) or "Изменений нет."


# ---------- задачи по расписанию ----------

async def quick() -> str:
    """Каждые 30 минут: новые посты в ленты, мёртвые статьи из блоков. Сама, без «Ок»."""
    cur = current()
    if not cur:
        return "хабы ещё не опубликованы — жду первой сборки (/hubs_build)"
    try:
        from core.seo import classify
        if not classify._running["on"]:
            await classify._classify()      # только новые, ещё не разложенные посты
    except Exception as e:
        log.warning("classify: %s", e)
    new = await _build(cur, repick=False)
    if _same(cur, new):
        return "без изменений"
    ok, bad = publish(new, "лента и уборка")
    if not ok:
        from core.seo import monitor
        await monitor.alert("hubs_guard", True, "хабы: обновление ленты не опубликовано — выглядит как поломка:\n"
                            + "\n".join(bad[:8]))
        return "остановлено защитой: " + "; ".join(bad[:3])
    from core.seo import monitor
    await monitor.alert("hubs_guard", False, "", "хабы снова обновляются")
    return f"опубликована v{current()['version']}: " + diff_text(cur, new).replace("\n", " ")[:200]


async def weekly(say=None) -> str:
    """Понедельник: новые подборки во всех блоках → черновик на «Ок» (или сразу, если HUBS_AUTO_APPLY)."""
    for key in pick.SUBPAGES:
        try:
            await pick.build(key)
        except Exception as e:
            log.warning("pick %s: %s", key, e)
            if say:
                await say(f"⚠️ Подборки {key} не собрались: {e}")
    cur = current()
    draft = await _build(cur, repick=True)
    text = diff_text(cur, draft)
    traffic = await _traffic()
    bad = _guard(cur, draft)
    head = "🗂 Хабы: подборки на неделю" + (f"\n\n{traffic}" if traffic else "")
    if bad:
        head += "\n\n⚠️ Защита заметила крупные изменения:\n" + "\n".join(bad[:8])
    if _same(cur, draft):
        if say:
            await say(head + "\n\nЗамен нет — оставляю как есть.")
        return "замен нет"
    report = config.OUTBOX / "seo-plan" / f"hubs-draft-{dt.date.today():%Y-%m-%d}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(head + "\n\n" + text, "utf-8")
    if config.HUBS_AUTO_APPLY and cur and not bad:
        publish(draft, "подборки недели (автоматически)")
        if say:
            await say(f"{head}\n\nОпубликовал v{current()['version']}:\n{text}"[:3900], [report])
        return "опубликовано автоматически"
    store.put("hubs_draft", {"doc": draft, "ts": time.time()})
    msg = f"{head}\n\n{text}"
    if len(msg) > 3500:
        msg = msg[:3400] + "\n…полностью — в файле."
    if _ask:
        await _ask(msg + "\n\nПрименить?", report)
    elif say:
        await say(msg, [report])
    return "черновик отправлен на «Ок»"


async def approve() -> str:
    d = store.get("hubs_draft")
    if not d:
        return "Черновика нет — он уже применён или заменён."
    doc = await _build(d["doc"], repick=False)      # лента и мёртвые статьи — на сегодня
    doc["version"] = (current() or {}).get("version", 0)
    publish(doc, "подборки недели (Ок)", force=True)
    store.put("hubs_draft", None)
    return f"✅ Опубликовал хабы v{current()['version']}. Сайт подхватит в течение часа."


def reject() -> str:
    store.put("hubs_draft", None)
    return "Ок, оставил прошлые подборки. Следующий черновик — в понедельник (или /hubs_build)."


def rollback(v: int) -> str:
    doc = version_doc(v)
    if not doc:
        return f"Версии {v} нет. Последние: " + ", ".join(str(x[0]) for x in versions())
    publish(doc, f"откат к v{v}", force=True)
    return f"↩️ Опубликовал содержимое v{v} как v{current()['version']}."


async def _traffic() -> str:
    """Визиты из поиска на страницы хабов за неделю к прошлой."""
    from core.seo import apis
    try:
        end = dt.date.today() - dt.timedelta(days=1)
        a = await apis.organic_by_page(end - dt.timedelta(days=6), end, 20000) or {}
        b = await apis.organic_by_page(end - dt.timedelta(days=13), end - dt.timedelta(days=7), 20000) or {}
    except Exception as e:
        log.warning("трафик хабов: %s", e)
        return ""
    s = lambda d: sum(v for u, v in d.items() if "/education" in u)
    na, nb = s(a), s(b)
    if not (na or nb):
        return "Из поиска на хабы за неделю: пока 0."
    return f"Из поиска на хабы за неделю: {na} (неделей раньше {nb})."


async def daily() -> str:
    """Страницы хабов на сайте живы и показывают свежую версию; статьи из блоков не удалены."""
    from core.seo import monitor
    cur = current()
    if not cur:
        return "хабы ещё не опубликованы"
    import aiohttp
    site = f"https://{config.SEO_SITE}"
    problems, seen_ok = [], False
    published_at = (versions(1) or [(0, time.time(), "")])[0][1]
    async with aiohttp.ClientSession(headers={"User-Agent": page.UA}) as s:
        for p in cur["pages"]:
            if p["status"] != "live":
                continue
            f = await page.fetch(site + p["path"], session=s, max_bytes=600_000, timeout=20)
            if f.status != 200:
                problems.append(f"{p['path']} — ответ {f.status}")
                continue
            seen_ok = True
            m = re.search(r'name=["\']hubs-version["\'][^>]*content=["\'](\d+)', f.html or "")
            if not m:
                problems.append(f"{p['path']} — нет метки hubs-version")
            elif int(m.group(1)) < cur["version"] and time.time() - published_at > 2 * 3600:
                problems.append(f"{p['path']} — на сайте v{m.group(1)}, опубликована v{cur['version']}")
        # статьи из блоков: перепроверяем, что живы
        dead = 0
        urls = {x["url"] for p in cur["pages"] for b in p["blocks"] for x in b["posts"]}
        for u in sorted(urls):
            f = await page.fetch(u, session=s, max_bytes=200_000, timeout=20)
            if f.status in (404, 410) or (f.status == 200 and re.search(
                    r'(?i)<meta[^>]+name=["\']robots["\'][^>]*noindex', f.html or "")):
                store.db().execute("UPDATE urls SET status=?, last_check=? WHERE url=?",
                                   (f.status if f.status != 200 else 0, time.time(), u))
                dead += 1
            await __import__("asyncio").sleep(1 / max(config.SEO_CRAWL_RPS, 0.1))
        store.db().commit()
    if seen_ok:
        store.put("hubs_site_live", True)
    note = []
    if dead:
        note.append(f"убрал удалённых или закрытых статей: {dead} — " + await quick())
    if store.get("hubs_site_live"):
        await monitor.alert("hubs_site", bool(problems), "хабы на сайте:\n" + "\n".join(problems[:10]),
                            "страницы хабов снова в порядке")
        note.append("сайт: " + ("; ".join(problems[:3]) if problems else "всё в порядке"))
    else:
        note.append("сайт ещё не подключил хабы (страницы не отвечают 200) — алерты включатся после подключения")
    return "; ".join(note)


async def monthly(say=None) -> str:
    """1-е число: данные по структуре (спрос, наши статьи, кандидаты) — предложения на «Ок»."""
    from core.seo import cluster
    files = []
    for direction in sorted({d for d, *_ in pick.SUBPAGES.values()}):
        try:
            files.append(await cluster.build(direction))
        except Exception as e:
            log.warning("cluster %s: %s", direction, e)
    if say and files:
        await say("🧭 Хабы: месячный пересмотр структуры. В файле — спрос по подтемам и намерениям, наши статьи и "
                  "«похожие запросы вне подтем» (кандидаты в новые блоки и подстраницы). Правило: новая подстраница — "
                  "спрос от 2–3 тыс. в месяц и от 15 наших статей. Скажи, что берём, — внесу и пришлю черновик на «Ок».",
                  [__import__("pathlib").Path(f) for f in files])
    return f"отчётов: {len(files)}"


def status_text() -> str:
    cur = current()
    lines = []
    if cur:
        live = [p for p in cur["pages"] if p["status"] == "live"]
        lines.append(f"Опубликована v{cur['version']} ({cur['generated_at'][:16].replace('T', ' ')}): страниц {len(live)}, "
                     f"блоков показано {sum(1 for p in live for b in p['blocks'] if not b['hidden'])}.")
    else:
        lines.append("Хабы ещё не опубликованы. Первая сборка — /hubs_build (черновик придёт на «Ок»).")
    if store.get("hubs_draft"):
        lines.append("Есть черновик, ждёт «Ок».")
    lines.append("Режим замен: " + ("автоматически" if config.HUBS_AUTO_APPLY else "после «Ок»") + ".")
    lines.append("Сайт: " + ("забирает и показывает" if store.get("hubs_site_live") else "ещё не подключён"))
    lines.append("Отдача: " + (f"порт {config.HUBS_PORT}, /hubs.json" if config.HUBS_TOKEN else "выключена — нет HUBS_TOKEN в .env"))
    if versions():
        lines.append("Версии: " + ", ".join(f"v{v} {note}" for v, _, note in versions(5)))
    return "\n".join(lines)


# ---------- отдача файла сайту ----------

async def _serve(req: web.Request):
    if req.headers.get("Authorization", "") != f"Bearer {config.HUBS_TOKEN}":
        return web.json_response({"error": "unauthorized"}, status=401)
    cur = current()
    if not cur:
        return web.json_response({"error": "not published yet"}, status=503)
    etag = f'"{cur["version"]}"'
    if req.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers={"ETag": etag})
    return web.json_response(_public(cur), headers={"ETag": etag, "Cache-Control": "no-cache"},
                             dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def start_server():
    if not config.HUBS_TOKEN:
        log.info("hubs.json не отдаётся: нет HUBS_TOKEN")
        return
    app = web.Application()
    app.router.add_get("/hubs.json", _serve)
    app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
    runner_ = web.AppRunner(app, access_log=None)
    await runner_.setup()
    await web.TCPSite(runner_, config.HUBS_HOST, config.HUBS_PORT).start()
    log.info("hubs.json слушает %s:%s", config.HUBS_HOST, config.HUBS_PORT)
