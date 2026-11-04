"""Раскладка всей базы постов по направлениям и подтемам хабов (один проход, дешёвая модель).

1. Для постов без заголовка в базе — лёгкий обход: title и description страницы (со скоростью SEO_CRAWL_RPS).
2. Пачками по 80 постов: заголовок + описание → модель выбирает направления и подтемы из списка inventory.T
   (или «нет», если пост не про эти темы: новости, интервью, мнения без предмета).
3. Результат — таблица post_topics в data/seo.db; /hub_inventory учитывает её вместе с поиском по словам.

Запуск: /hub_classify в боте (продолжается с места остановки, повторный запуск доделывает новое).
Цена: ~$1–4 за всю базу (10 тыс. постов) в зависимости от модели SEO_CLASSIFY_MODEL.
"""
import asyncio
import json
import logging
import re
import time
from html import unescape

import aiohttp
import anthropic

from core import config, spend
from core.seo import inventory, page, store

log = logging.getLogger("seo.classify")
BATCH = 80
_running = {"on": False, "phase": "", "done": 0, "total": 0, "started": 0.0, "error": ""}


def _db():
    d = store.db()
    d.execute("CREATE TABLE IF NOT EXISTS post_topics(url TEXT PRIMARY KEY, directions TEXT, subtopics TEXT, ts REAL)")
    return d


def labels() -> list[str]:
    return [f"{d} / {s}" for d, subs in inventory.T.items() for s in subs]


def results() -> dict[str, list[str]]:
    """{url: ["Брендинг / Айдентика", ...]}"""
    return {r[0]: json.loads(r[1] or "[]") for r in _db().execute("SELECT url, subtopics FROM post_topics")}


def status() -> str:
    n = _db().execute("SELECT COUNT(*) FROM post_topics").fetchone()[0]
    total = len(inventory._posts())
    s = f"Разложено постов: {n} из {total}."
    if _running["on"]:
        mins = (time.time() - _running["started"]) / 60
        s += f"\nИдёт: {_running['phase']} — {_running['done']}/{_running['total']} ({mins:.0f} мин)."
    if _running["error"]:
        s += f"\nПоследняя ошибка: {_running['error']}"
    return s


def _head(html: str) -> tuple[str, str]:
    t = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    d = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)', html) or \
        re.search(r'(?is)<meta[^>]+property=["\']og:description["\'][^>]*content=["\']([^"\']*)', html)
    title = unescape(t.group(1)).strip() if t else ""
    title = re.sub(r"\s*[—–-]\s*дизайнерс\s*$", "", title)
    return title, unescape(d.group(1)).strip() if d else ""


async def _fill_titles(say=None):
    d = store.db()
    rows = [r for r in d.execute("SELECT url, title, description, sm FROM urls WHERE in_sitemap=1")
            if ("posts" in (r["sm"] or "") or (not r["sm"] and re.search(r"/[^/]+/\d+-", r["url"])))
            and not (r["title"] and r["description"])]
    _running.update(phase="читаю заголовки страниц", done=0, total=len(rows))
    if not rows:
        return
    delay = 1 / max(config.SEO_CRAWL_RPS, 0.1)
    async with aiohttp.ClientSession(headers={"User-Agent": page.UA}) as s:
        for i, r in enumerate(rows):
            f = await page.fetch(r["url"], session=s, max_bytes=300_000, timeout=20)
            if f.status == 200 and f.html:
                title, desc = _head(f.html)
                d.execute("UPDATE urls SET title=CASE WHEN title='' OR title IS NULL THEN ? ELSE title END, "
                          "description=CASE WHEN description='' OR description IS NULL THEN ? ELSE description END WHERE url=?",
                          (title, desc[:300], r["url"]))
            if i % 50 == 0:
                d.commit()
            _running["done"] = i + 1
            if say and i and i % 2000 == 0:
                await say(f"🗂 Заголовки: {i} из {len(rows)}")
            await asyncio.sleep(delay)
    d.commit()


TOOL = {
    "name": "topics",
    "description": "Направления и подтемы для каждого поста из пачки.",
    "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {
            "i": {"type": "integer"},
            "labels": {"type": "array", "items": {"type": "string"},
                       "description": "0–3 метки ТОЧНО из списка; пустой список — пост не про эти темы"},
        }, "required": ["i", "labels"]}}}, "required": ["items"]},
}


def _items(out) -> list[dict]:
    """Модель иногда отдаёт items строкой JSON или элементы строками — разбираем всё, что похоже на ответ."""
    items = out.get("items", []) if isinstance(out, dict) else out
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            return []
    res = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            try:
                it = json.loads(it)
            except ValueError:
                continue
        if isinstance(it, dict):
            labs = it.get("labels", [])
            if isinstance(labs, str):
                labs = [x.strip() for x in labs.split(",") if x.strip()]
            res.append({"i": it.get("i"), "labels": labs if isinstance(labs, list) else []})
    return res


async def _classify(say=None):
    d = _db()
    done = {r[0] for r in d.execute("SELECT url FROM post_topics")}
    meta = {r["url"]: (r["title"] or "", r["description"] or "") for r in store.db().execute(
        "SELECT url, title, description FROM urls")}
    todo = [p for p in inventory._posts() if p["url"] not in done]
    _running.update(phase="раскладываю по темам", done=0, total=len(todo))
    allowed = labels()
    system = ("Ты раскладываешь посты дизайн-платформы дизайнерс по направлениям и подтемам. Для каждого поста выбери "
              "0–3 метки строго из списка — только если пост действительно про это (кейс, урок, разбор, подборка, мнение по теме). "
              "Кейс студии по айдентике бренда — «Брендинг / Айдентика»; кейс сайта — «UI / Сайты и лендинги»; "
              "новость без предмета, интервью о жизни, анонс мероприятия — пустой список. Метки:\n" + "\n".join(allowed))
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    model = config.SEO_CLASSIFY_MODEL or config.MODEL
    for k in range(0, len(todo), BATCH):
        chunk = todo[k:k + BATCH]
        lines = []
        for j, p in enumerate(chunk):
            t, ds = meta.get(p["url"], ("", ""))
            slug = p["url"].rstrip("/").rsplit("/", 1)[-1]
            lines.append(f"{j}. {t or slug} — {ds[:200]}")
        try:
            resp = await client.messages.create(
                model=model, max_tokens=4000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=[TOOL], tool_choice={"type": "tool", "name": "topics"},
                messages=[{"role": "user", "content": "Посты:\n" + "\n".join(lines)}])
            spend.record("seo", resp.usage, model)
        except Exception as e:
            _running["error"] = str(e)[:200]
            log.warning("классификация: %s", e)
            await asyncio.sleep(10)
            continue
        out = next((b.input for b in resp.content if b.type == "tool_use"), {}) or {}
        ok = set(allowed)
        for it in _items(out):
            i = it.get("i")
            if not isinstance(i, int) or not 0 <= i < len(chunk):
                continue
            labs = [x for x in it.get("labels", []) if x in ok][:3]
            dirs = sorted({x.split(" / ")[0] for x in labs})
            d.execute("INSERT OR REPLACE INTO post_topics(url, directions, subtopics, ts) VALUES(?,?,?,?)",
                      (chunk[i]["url"], json.dumps(dirs, ensure_ascii=False), json.dumps(labs, ensure_ascii=False), time.time()))
        d.commit()
        _running["done"] = min(k + BATCH, len(todo))
        if say and k and k % 2000 < BATCH:
            await say(f"🗂 Разложено {k + len(chunk)} из {len(todo)}")


async def run(say=None) -> str:
    if _running["on"]:
        return "Уже идёт.\n" + status()
    _running.update(on=True, started=time.time(), error="")
    try:
        await _fill_titles(say)
        await _classify(say)
    finally:
        _running["on"] = False
    return "Готово.\n" + status()
