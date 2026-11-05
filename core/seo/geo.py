"""GEO — видимость дизайнерс в нейроответах (ТЗ: geo-monitoring.md в проекте).

Раз в неделю (понедельник, после полного обхода) прогоняем набор запросов через:
- yandex     — генеративный ответ Yandex Search API (POST /v2/gen/search по всему индексу) — приближение к Алисе/Нейро;
               тем же ключом — обычная выдача (POST /v2/web/search), чтобы понять «в топ-10, но не в ответе»;
- perplexity — Perplexity API (sonar), если есть PERPLEXITY_API_KEY;
- openai     — OpenAI Responses API + web_search, если есть OPENAI_API_KEY.
Интерфейсы Алисы/ChatGPT не парсим — только официальные API.

Без ИИ Claude: стоит только запросы к API движков. Ответы кешируются на 3 дня.

.env: GEO_ENABLED=1, GEO_ENGINES=yandex[,perplexity,openai], GEO_QUERIES_MAX=50,
      PERPLEXITY_API_KEY, OPENAI_API_KEY, GEO_OPENAI_MODEL; ключ Яндекса — WORDSTAT_API_KEY + YC_FOLDER_ID.
"""
import asyncio
import base64
import datetime as dt
import hashlib
import json
import logging
import re
import time
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import aiohttp

from core import config
from core.seo import store

log = logging.getLogger("seo.geo")

YA = "https://searchapi.api.cloud.yandex.net/v2"
TTL = 3 * 86400
PAUSE = 1.2                         # генеративный поиск Яндекса — не больше 1 запроса в секунду
ENGINE_NAMES = {"yandex": "Яндекс (API)", "perplexity": "Perplexity", "openai": "ChatGPT (API)"}
TYPES = {"info": "информационный", "where": "подборка / «где»", "brand": "брендовый"}
BRAND_SEED = ["дизайнерс", "дизайнерс что это за платформа", "dsgners", "dsgners отзывы", "платформа для дизайнеров дизайнерс"]
WHERE_SEED = ["где выложить кейс дизайнеру", "сообщество дизайнеров", "где публиковать статьи о дизайне",
              "платформа для дизайнеров", "где почитать про дизайн"]
NO_ANSWER = ("ничего не нашлось", "не удалось извлечь информацию")

# ИИ-боты, которых нельзя закрывать в robots.txt
AI_BOTS = ["YandexBot", "Googlebot", "Bingbot", "OAI-SearchBot", "GPTBot", "PerplexityBot", "ClaudeBot"]

# Сегмент «ИИ-ассистенты» в Метрике: домены, с которых приходят переходы
AI_REFERRERS = ["chatgpt.com", "chat.openai.com", "perplexity.ai", "gemini.google.com", "copilot.microsoft.com",
                "giga.chat", "claude.ai", "chat.deepseek.com", "alice.yandex.ru"]


def engines() -> list[str]:
    out = []
    for e in config.GEO_ENGINES:
        if e == "yandex" and config.WORDSTAT_API_KEY and config.YC_FOLDER_ID:
            out.append(e)
        elif e == "perplexity" and config.PERPLEXITY_API_KEY:
            out.append(e)
        elif e == "openai" and config.OPENAI_API_KEY:
            out.append(e)
    return out


def enabled() -> bool:
    return config.GEO_ENABLED and bool(engines())


def why_off() -> str:
    if not config.GEO_ENABLED:
        return "GEO_ENABLED=0 в .env"
    return ("нет ключей движков: для Яндекса — WORDSTAT_API_KEY и YC_FOLDER_ID, "
            "для Perplexity/OpenAI — их ключи и GEO_ENGINES")


# ---------------- база ----------------

def _db():
    d = store.db()
    d.executescript("""
    CREATE TABLE IF NOT EXISTS geo_queries(query TEXT PRIMARY KEY, type TEXT, source TEXT, added REAL);
    CREATE TABLE IF NOT EXISTS geo_results(
        week TEXT, engine TEXT, query TEXT, type TEXT, answered INTEGER, cited INTEGER, cited_url TEXT,
        cited_rank INTEGER, own INTEGER, sources TEXT, serp_pos INTEGER, serp_url TEXT, error TEXT, ts REAL,
        PRIMARY KEY(week, engine, query));
    CREATE TABLE IF NOT EXISTS geo_cache(key TEXT PRIMARY KEY, data TEXT, ts REAL);
    """)
    return d


def norm_q(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().strip("«»\"'").lower())


def queries() -> list[dict]:
    return [dict(r) for r in _db().execute("SELECT query, type, source FROM geo_queries ORDER BY type, added")]


def add_query(q: str, qtype: str = "", source: str = "manual") -> str:
    q = norm_q(q)
    if not q:
        return "пустой запрос"
    if len(queries()) >= config.GEO_QUERIES_MAX and not _db().execute(
            "SELECT 1 FROM geo_queries WHERE query=?", (q,)).fetchone():
        return f"в наборе уже {config.GEO_QUERIES_MAX} запросов (GEO_QUERIES_MAX) — сначала удали лишние"
    qtype = qtype or guess_type(q)
    _db().execute("INSERT OR REPLACE INTO geo_queries(query,type,source,added) VALUES(?,?,?,?)",
                  (q, qtype, source, time.time()))
    _db().commit()
    return f"добавил «{q}» ({TYPES.get(qtype, qtype)})"


def del_query(q: str) -> bool:
    cur = _db().execute("DELETE FROM geo_queries WHERE query=?", (norm_q(q),))
    _db().commit()
    return cur.rowcount > 0


def guess_type(q: str) -> str:
    if "дизайнерс" in q or "dsgners" in q:
        return "brand"
    if re.search(r"\b(где|сообществ|платформ|сайт[ы]? для|подборк|лучшие|топ)\b", q):
        return "where"
    return "info"


def seed(force: bool = False) -> str:
    """Стартовый набор: брендовые + «где» + запросы Вебмастера, где мы на 1–30 месте, + базовые фразы Вордстата.
    Ручные запросы (source=manual) не трогаем. force — пересобрать автоматическую часть (раз в месяц)."""
    if queries() and not force:
        return f"набор уже есть: {len(queries())} запросов"
    if force:
        _db().execute("DELETE FROM geo_queries WHERE source IN ('webmaster','wordstat')")
        _db().commit()
    have = {r["query"] for r in queries()}
    added = []

    def put(q, t, src):
        q = norm_q(q)
        if q and q not in have and len(have) < config.GEO_QUERIES_MAX:
            _db().execute("INSERT OR IGNORE INTO geo_queries(query,type,source,added) VALUES(?,?,?,?)",
                          (q, t, src, time.time()))
            have.add(q)
            added.append(q)

    for q in BRAND_SEED:
        put(q, "brand", "fixed")
    for q in WHERE_SEED:
        put(q, "where", "fixed")
    wm = store.get("wm_queries") or []
    near = sorted((x for x in wm if x.get("pos", 99) <= 30 and x.get("shows", 0) >= 20
                   and len(x.get("query", "").split()) >= 2 and guess_type(norm_q(x["query"])) != "brand"),
                  key=lambda x: -x["shows"])
    for x in near[:25]:
        put(x["query"], guess_type(norm_q(x["query"])), "webmaster")
    for q in config.WORDSTAT_SEEDS:
        put(q, "info", "wordstat")
    _db().commit()
    store.put("geo_seeded", time.time())
    return f"в наборе {len(have)} запросов, добавлено {len(added)}"


# ---------------- кеш ----------------

def _ckey(engine: str, q: str) -> str:
    return engine + ":" + hashlib.sha1(q.encode()).hexdigest()


def _cache_get(key: str):
    r = _db().execute("SELECT data, ts FROM geo_cache WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r and time.time() - r[1] < TTL else None


def _cache_put(key: str, data):
    _db().execute("INSERT OR REPLACE INTO geo_cache(key,data,ts) VALUES(?,?,?)",
                  (key, json.dumps(data, ensure_ascii=False), time.time()))
    _db().commit()


# ---------------- движки ----------------

_ya_lock = asyncio.Lock()
_ya_last = 0.0


async def _ya_post(path: str, body: dict) -> dict:
    global _ya_last
    headers = {"Authorization": f"Api-Key {config.WORDSTAT_API_KEY}"}
    body = {**body, "folderId": config.YC_FOLDER_ID}
    err = ""
    for attempt in range(3):
        async with _ya_lock:
            wait = PAUSE - (time.monotonic() - _ya_last)
            if wait > 0:
                await asyncio.sleep(wait)
            _ya_last = time.monotonic()
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
                async with s.post(f"{YA}/{path}", headers=headers, json=body) as r:
                    txt = await r.text()
                    status = r.status
        if status < 400:
            return _json_or_lines(txt)
        err = f"HTTP {status}: {txt[:300]}"
        if status not in (429, 500, 502, 503, 504):
            break
        await asyncio.sleep(3 * (attempt + 1))
    raise RuntimeError(err)


def _json_or_lines(txt: str) -> dict:
    """Ответ — один JSON; на всякий случай понимаем и JSON Lines/массив (берём последний, самый полный объект)."""
    txt = txt.strip()
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        last = {}
        for line in txt.splitlines():
            line = line.strip().rstrip(",")
            if line.startswith("{"):
                try:
                    last = json.loads(line)
                except json.JSONDecodeError:
                    pass
        return last
    if isinstance(data, list):
        data = data[-1] if data else {}
    return data.get("result", data) if isinstance(data, dict) else {}


def _src_urls(sources) -> list[str]:
    """Источники ответа в порядке появления; если есть флаг used — только использованные."""
    items = [s for s in (sources or []) if isinstance(s, dict) and s.get("url")]
    used = [s for s in items if s.get("used")]
    return [s["url"] for s in (used or items)]


async def yandex(q: str) -> dict:
    """Генеративный ответ Яндекса по всему индексу (site/host/url не задаём)."""
    data = await _ya_post("gen/search", {
        "messages": [{"content": q, "role": "ROLE_USER"}],
        "searchType": "SEARCH_TYPE_RU",
        "fixMisspell": True,
    })
    msg = data.get("message") or {}
    text = (msg.get("content") if isinstance(msg, dict) else str(msg)) or ""
    rejected = bool(data.get("isAnswerRejected")) or any(s in text.lower()[:200] for s in NO_ANSWER)
    return {"answered": bool(text) and not rejected, "text": text, "sources": _src_urls(data.get("sources"))}


async def yandex_serp(q: str) -> list[str]:
    """Обычная выдача Яндекса, топ-10 адресов."""
    data = await _ya_post("web/search", {
        "query": {"searchType": "SEARCH_TYPE_RU", "queryText": q, "familyMode": "FAMILY_MODE_MODERATE"},
        "groupSpec": {"groupMode": "GROUP_MODE_DEEP", "groupsOnPage": "10", "docsInGroup": "1"},
        "responseFormat": "FORMAT_XML",
    })
    raw = data.get("rawData") or ""
    xml = base64.b64decode(raw).decode("utf-8", "replace") if raw else ""
    if not xml:
        return []
    root = ET.fromstring(xml)
    err = root.find(".//response/error")
    if err is not None:
        txt = (err.text or "").strip()
        if "не нашлось" in txt.lower() or err.get("code") == "15":
            return []
        raise RuntimeError(f"выдача: {txt[:200]}")
    return [u.text.strip() for u in root.iter("url") if u.text][:10]


async def perplexity(q: str) -> dict:
    body = {"model": config.GEO_PERPLEXITY_MODEL, "messages": [{"role": "user", "content": q}]}
    headers = {"Authorization": f"Bearer {config.PERPLEXITY_API_KEY}"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        async with s.post("https://api.perplexity.ai/chat/completions", headers=headers, json=body) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {str(data)[:300]}")
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    urls = list(data.get("citations") or []) or [x.get("url") for x in data.get("search_results") or [] if x.get("url")]
    return {"answered": bool(text), "text": text, "sources": urls}


async def openai(q: str) -> dict:
    body = {"model": config.GEO_OPENAI_MODEL, "input": q, "tools": [{"type": "web_search"}]}
    headers = {"Authorization": f"Bearer {config.OPENAI_API_KEY}"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.post("https://api.openai.com/v1/responses", headers=headers, json=body) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {str(data)[:300]}")
    text, urls = [], []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            text.append(c.get("text") or "")
            for a in c.get("annotations") or []:
                if a.get("type") == "url_citation" and a.get("url") and a["url"] not in urls:
                    urls.append(a["url"])
    t = "\n".join(text)
    return {"answered": bool(t), "text": t, "sources": urls}


ENGINES = {"yandex": yandex, "perplexity": perplexity, "openai": openai}


# ---------------- разбор ----------------

def host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def is_ours(u: str) -> bool:
    h = host(u)
    site = config.SEO_SITE.lower()
    return h == site or h.endswith("." + site)


def _clean(u: str) -> str:
    return u.split("#")[0].split("?utm_")[0]


async def check(q: str, engine: str, use_cache: bool = True) -> dict:
    """Один запрос в одном движке: ответ, источники, цитируют ли нас, наша позиция в обычной выдаче (для Яндекса)."""
    from core.seo import monitor
    q = norm_q(q)
    key = _ckey(engine, q)
    res = _cache_get(key) if use_cache else None
    if res is None:
        res = {"engine": engine, "query": q, "error": ""}
        try:
            res.update(await ENGINES[engine](q))
        except Exception as e:
            res.update({"answered": False, "text": "", "sources": [], "error": str(e)[:300]})
        if engine == "yandex":
            try:
                serp = await yandex_serp(q)
                res["serp"] = serp
            except Exception as e:
                res["serp"] = None
                res["serp_error"] = str(e)[:200]
        if not res["error"]:
            _cache_put(key, res)
    srcs = [_clean(u) for u in res.get("sources") or []]
    ours = [(i + 1, u) for i, u in enumerate(srcs) if is_ours(u)]
    res["cited"] = bool(ours)
    res["cited_rank"], res["cited_url"] = ours[0] if ours else (0, "")
    serp = res.get("serp") or []
    pos = [(i + 1, u) for i, u in enumerate(serp) if is_ours(u)]
    res["serp_pos"], res["serp_url"] = pos[0] if pos else (0, "")
    page_url = res["cited_url"] or res["serp_url"]
    res["own"] = monitor.is_own(page_url) if page_url else None
    res["domains"] = list(dict.fromkeys(host(u) for u in srcs if host(u)))
    return res


def fmt_one(r: dict) -> str:
    name = ENGINE_NAMES.get(r["engine"], r["engine"])
    if r.get("error"):
        return f"🤖 {name}: проверка не прошла — {r['error'][:200]}"
    L = [f"🤖 {name} — «{r['query']}»"]
    if not r.get("answered"):
        L.append("Нейроответа нет (ничего не нашлось или не удалось собрать ответ).")
    else:
        snippet = re.sub(r"\s+", " ", r.get("text", ""))[:500]
        L.append(f"Ответ: {snippet}{'…' if len(r.get('text', '')) > 500 else ''}")
        L.append(("✅ Цитирует дизайнерс: " + f"#{r['cited_rank']} {r['cited_url']}") if r["cited"]
                 else "❌ Дизайнерс не цитирует")
        if r.get("domains"):
            L.append("Источники: " + ", ".join(r["domains"][:10]))
    if r["engine"] == "yandex":
        if r.get("serp") is None:
            L.append(f"Обычная выдача: не проверилась ({r.get('serp_error', '')[:120]})")
        elif r["serp_pos"]:
            who = " (редакция)" if r.get("own") else " (статья автора)" if r.get("own") is False else ""
            L.append(f"Обычная выдача: мы на {r['serp_pos']} месте — {r['serp_url']}{who}")
        else:
            L.append("Обычная выдача: нас нет в топ-10")
    return "\n".join(L)


# ---------------- недельный прогон ----------------

def week_key(d: dt.date | None = None) -> str:
    d = d or dt.date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _save(week: str, r: dict, qtype: str):
    _db().execute("""INSERT OR REPLACE INTO geo_results(week,engine,query,type,answered,cited,cited_url,cited_rank,own,
                     sources,serp_pos,serp_url,error,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (week, r["engine"], r["query"], qtype, int(bool(r.get("answered"))), int(r["cited"]), r["cited_url"],
                   r["cited_rank"], None if r.get("own") is None else int(r["own"]),
                   json.dumps(r.get("domains") or [], ensure_ascii=False),
                   r["serp_pos"] if r.get("serp") is not None else None, r["serp_url"], r.get("error") or "", time.time()))
    _db().commit()


def _rows(week: str) -> list[dict]:
    return [dict(r) for r in _db().execute("SELECT * FROM geo_results WHERE week=?", (week,))]


def weeks(n: int = 8) -> list[str]:
    return [r[0] for r in _db().execute("SELECT DISTINCT week FROM geo_results ORDER BY week DESC LIMIT ?", (n,))]


async def run_all(progress=None) -> dict:
    """Прогнать весь набор по всем включённым движкам. Вернёт {week, checked, errors}."""
    if not queries():
        seed()
    week = week_key()
    qs = queries()
    errors, checked = 0, 0
    for e in engines():
        for i, q in enumerate(qs):
            r = await check(q["query"], e)
            _save(week, r, q["type"])
            checked += 1
            errors += bool(r.get("error"))
            if progress and i % 10 == 9:
                await progress(f"GEO: {ENGINE_NAMES[e]} — {i + 1}/{len(qs)}")
    store.put("geo_last", {"week": week, "ts": time.time(), "checked": checked, "errors": errors})
    return {"week": week, "checked": checked, "errors": errors}


def _share(rows: list[dict], engine: str | None = None) -> tuple[int, int]:
    ok = [r for r in rows if not r["error"] and (engine is None or r["engine"] == engine)]
    if engine is None:      # общий итог — по запросам: цитирует хоть один движок
        by_q = {}
        for r in ok:
            by_q[r["query"]] = by_q.get(r["query"], False) or bool(r["cited"])
        return sum(by_q.values()), len(by_q)
    return sum(r["cited"] for r in ok), len(ok)


def _pct(a: int, b: int) -> str:
    return f"{a} из {b} ({a * 100 // b}%)" if b else "нет данных"


def candidates(rows: list[dict]) -> list[dict]:
    """В топ-10 обычной выдачи Яндекса, но нейроответ Яндекса нас не цитирует — главные кандидаты на доработку."""
    out = [r for r in rows if r["engine"] == "yandex" and not r["error"] and r["serp_pos"] and r["answered"]
           and not r["cited"]]
    return sorted(out, key=lambda r: r["serp_pos"])


async def ai_traffic_line() -> str:
    from core.seo import apis
    try:
        end = dt.date.today() - dt.timedelta(days=1)
        cur = await apis.ai_traffic(end - dt.timedelta(days=6), end)
        prev = await apis.ai_traffic(end - dt.timedelta(days=13), end - dt.timedelta(days=7))
    except Exception as e:
        return f"Трафик из ИИ: Метрика не ответила ({str(e)[:120]})"
    if cur is None:
        return "Трафик из ИИ: Метрика не подключена"
    tot, pages = cur
    ptot = prev[0] if prev else 0
    ch = f" ({'+' if tot >= ptot else ''}{(tot - ptot) * 100 // ptot}%)" if ptot else ""
    top = ", ".join(f"{u} — {v}" for u, v in list(pages.items())[:5])
    return (f"Трафик из ИИ-ассистентов за неделю: {tot} визитов{ch}, неделю назад {ptot}"
            + (f"\nТоп страниц входа: {top}" if top else "")
            + "\n_Переходы из Нейро внутри поиска Яндекса Метрика отдельно не показывает — они в обычном поиске._")


async def report(week: str | None = None) -> tuple[str, list[dict]]:
    """Блок GEO для недельного отчёта. Вернёт (текст, кандидаты)."""
    ws = weeks(2)
    week = week or (ws[0] if ws else week_key())
    rows = _rows(week)
    if not rows:
        return "🤖 Нейроответы: проверок ещё не было — /geo_check", []
    prev_w = next((w for w in weeks(10) if w < week), None)
    prev = _rows(prev_w) if prev_w else []
    errs = [r for r in rows if r["error"]]
    good = [r for r in rows if not r["error"]]
    L = [f"🤖 Нейроответы — неделя {week}", ""]
    if not good:
        L.append("Проверка не прошла: все запросы вернули ошибку — доли не считаю.")
        L.append("Ошибка: " + errs[0]["error"][:300])
        return "\n".join(L), []
    a, b = _share(rows)
    line = f"Доля цитирований: {_pct(a, b)}"
    if prev:
        pa, pb = _share(prev)
        line += f", неделю назад {_pct(pa, pb)}"
    L.append(line)
    per = [f"{ENGINE_NAMES.get(e, e)}: {_share(rows, e)[0]}/{_share(rows, e)[1]}"
           for e in sorted({r["engine"] for r in good})]
    L.append("∙ " + " ∙ ".join(per))
    if errs:
        L.append(f"Не прошли проверку: {len(errs)} (ошибки API) — в доли не входят.")

    if prev:
        was = {(r["engine"], r["query"]): r for r in prev if not r["error"]}
        appeared = [r for r in good if r["cited"] and (r["engine"], r["query"]) in was
                    and not was[(r["engine"], r["query"])]["cited"]]
        lost = [r for r in good if not r["cited"] and was.get((r["engine"], r["query"]), {}).get("cited")]
        if appeared:
            L.append("\nПоявились:")
            L += [f"∙ «{r['query']}» ({ENGINE_NAMES.get(r['engine'])}) → {r['cited_url']}" for r in appeared[:10]]
        if lost:
            L.append("\nПропали:")
            for r in lost[:10]:
                dom = ", ".join(json.loads(r["sources"] or "[]")[:3])
                L.append(f"∙ «{r['query']}» ({ENGINE_NAMES.get(r['engine'])})" + (f" — вместо нас {dom}" if dom else ""))

    cand = candidates(rows)
    if cand:
        L.append("\nВ топ-10, но не в нейроответе Яндекса (кандидаты на доработку, /geo_fix N):")
        for i, r in enumerate(cand[:10], 1):
            who = "редакция" if r["own"] else "статья автора" if r["own"] == 0 else ""
            L.append(f"{i}. «{r['query']}» — поз. {r['serp_pos']}, {r['serp_url']}" + (f" ({who})" if who else ""))
    else:
        L.append("\nЗапросов «в топ-10, но не в ответе» нет.")

    dom = {}
    for r in good:
        if r["cited"]:
            continue
        for d in json.loads(r["sources"] or "[]"):
            if d and not is_ours("https://" + d):
                dom[d] = dom.get(d, 0) + 1
    if dom:
        top = sorted(dom.items(), key=lambda x: -x[1])[:8]
        L.append("\nКто забирает цитирования (там, где нас нет): " + ", ".join(f"{d} {n}" for d, n in top))
    L.append("\n" + await ai_traffic_line())
    L.append("\n_Ответ Яндекса — генеративный поиск Search API, приближение к Алисе в поиске, а не её точная копия._")
    store.put("geo_candidates", [{"query": r["query"], "url": r["serp_url"], "pos": r["serp_pos"], "own": r["own"]}
                                 for r in cand[:10]])
    return "\n".join(L), cand


def history_text(n: int = 8) -> str:
    ws = list(reversed(weeks(n)))
    if not ws:
        return "Истории ещё нет."
    L = ["Доля цитирований по неделям:"]
    for w in ws:
        a, b = _share(_rows(w))
        L.append(f"{w}: {_pct(a, b)}")
    return "\n".join(L)


def plan_section() -> str:
    """Для данных контент-плана: где мы в топе, но не в ответе, и где нейроответ есть, а нас в выдаче нет."""
    ws = weeks(1)
    if not ws:
        return ""
    rows = [r for r in _rows(ws[0]) if r["engine"] == "yandex" and not r["error"]]
    cand = candidates(rows)
    missing = [r for r in rows if r["answered"] and not r["serp_pos"] and not r["cited"] and r["type"] != "brand"]
    L = []
    if cand:
        L.append("## GEO: в топ-10 Яндекса, но нейроответ нас не цитирует — доработать (лид-ответ, факты, вопросы)")
        L += [f"- «{r['query']}» — поз. {r['serp_pos']}, {r['serp_url']}" for r in cand[:15]]
        L.append("")
    if missing:
        L.append("## GEO: нейроответ есть, а нас нет ни в ответе, ни в топ-10 — кандидаты «🤖 на цитирование»")
        L += [f"- «{r['query']}» — цитируют: {', '.join(json.loads(r['sources'] or '[]')[:4])}" for r in missing[:15]]
        L.append("")
    return "\n".join(L)


# ---------------- robots.txt и ИИ-боты ----------------

def blocked_bots(robots: str, bots: list[str] = AI_BOTS) -> list[str]:
    """Каким ботам robots.txt закрывает весь сайт (Disallow: / без Allow: /).
    Своя группа бота важнее группы «*», как у поисковиков."""
    groups, agents, rules, last_was_agent = [], [], [], False
    for line in robots.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if not last_was_agent and agents:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(v.lower())
            last_was_agent = True
        elif k in ("allow", "disallow"):
            rules.append((k, v))
            last_was_agent = False
        else:
            last_was_agent = False
    if agents:
        groups.append((agents, rules))

    def closes(rules):
        dis = any(k == "disallow" and v == "/" for k, v in rules)
        allow_root = any(k == "allow" and v in ("/", "/*", "*") for k, v in rules)
        return dis and not allow_root

    out = []
    for b in bots:
        own = [r for a, r in groups if b.lower() in a]
        star = [r for a, r in groups if "*" in a]
        use = own or star
        if any(closes(r) for r in use):
            out.append(b)
    return out
