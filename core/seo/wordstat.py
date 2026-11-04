"""Вордстат по API (Yandex Cloud / AI Studio, Search API v2 → Wordstat).

Что умеет:
- top        — популярные запросы со словами фразы за 30 дней + похожие запросы (до 2000 фраз);
- dynamics   — частота фразы во времени: по месяцам (2 года), неделям (полгода) или дням (2 месяца) + тренд;
- regions    — где фразу ищут чаще: регионы/города, доля и индекс интереса (>100 — выше среднего по стране);
- compare    — сравнить несколько фраз: частота за 30 дней, рост за год и за 3 месяца, пик сезона;
- parse      — собрать семантику: топ по фразе + похожие (и на шаг глубже) → в базу keywords SEO-шника.

Доступ (.env):
WORDSTAT_API_KEY   — API-ключ сервисного аккаунта (роль search-api.webSearch.user, область yc.search-api.execute)
YC_FOLDER_ID       — ID каталога в Yandex Cloud, где создан сервисный аккаунт
WORDSTAT_REGIONS   — регионы по умолчанию через запятую (225 — Россия); пусто — все регионы
WORDSTAT_SEEDS     — базовые фразы для еженедельного сбора семантики перед контент-планом

Частоты в Вордстате «широкие»: все запросы, где есть слова фразы, в любом порядке и форме.
Ответы кешируются в data/seo.db (3 дня, дерево регионов — 30 дней), чтобы не долбить API одним и тем же.
"""
import asyncio
import datetime as dt
import hashlib
import json
import logging
import time

import aiohttp

from core import config
from core.seo import keywords, store

BASE = "https://searchapi.api.cloud.yandex.net/v2/wordstat"
DEVICES = {"all": "DEVICE_ALL", "desktop": "DEVICE_DESKTOP", "phone": "DEVICE_PHONE", "tablet": "DEVICE_TABLET"}
TTL = 3 * 86400
log = logging.getLogger("seo.wordstat")
_lock = asyncio.Lock()
_last = 0.0


def enabled() -> bool:
    return bool(config.WORDSTAT_API_KEY and config.YC_FOLDER_ID)


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------- кеш ----------
def _cdb():
    d = store.db()
    d.execute("CREATE TABLE IF NOT EXISTS ws_cache(key TEXT PRIMARY KEY, data TEXT, ts REAL)")
    return d


def _ckey(path: str, body: dict) -> str:
    b = {k: v for k, v in body.items() if k != "folderId"}
    return path + ":" + hashlib.sha1(json.dumps(b, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _cache_get(key: str, ttl: float):
    r = _cdb().execute("SELECT data, ts FROM ws_cache WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r and time.time() - r[1] < ttl else None


def _cache_put(key: str, data):
    _cdb().execute("INSERT OR REPLACE INTO ws_cache(key,data,ts) VALUES(?,?,?)",
                   (key, json.dumps(data, ensure_ascii=False), time.time()))
    _cdb().commit()


# ---------- запрос ----------
async def _post(path: str, body: dict, ttl: float = TTL) -> dict:
    """POST в Wordstat API: с кешем, не чаще WORDSTAT_RPS в секунду, повтор при 429/5xx."""
    global _last
    if not enabled():
        raise RuntimeError("Вордстат API не подключён: нужны WORDSTAT_API_KEY и YC_FOLDER_ID в .env")
    body = {**body, "folderId": config.YC_FOLDER_ID}
    key = _ckey(path, body)
    if ttl and (hit := _cache_get(key, ttl)) is not None:
        return hit
    headers = {"Authorization": f"Api-Key {config.WORDSTAT_API_KEY}"}
    err = ""
    for attempt in range(4):
        async with _lock:
            wait = 1 / max(config.WORDSTAT_RPS, 0.1) - (time.monotonic() - _last)
            if wait > 0:
                await asyncio.sleep(wait)
            _last = time.monotonic()
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
                async with s.post(f"{BASE}/{path}", headers=headers, json=body) as r:
                    data = await r.json(content_type=None)
                    status = r.status
        if status < 400:
            if ttl:
                _cache_put(key, data)
            return data
        err = f"HTTP {status}: {str(data)[:300]}"
        if status not in (429, 500, 502, 503, 504):
            break
        await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Вордстат API: {err}")


def _regions(regions) -> list[str]:
    if regions is None:
        regions = config.WORDSTAT_REGIONS
    if isinstance(regions, str):
        regions = [x.strip() for x in regions.split(",")]
    return [str(r) for r in regions if str(r).strip()]


def _devices(device: str | None) -> list[str]:
    return [DEVICES.get((device or "all").lower(), "DEVICE_ALL")]


def _clean(phrase: str) -> str:
    return " ".join(phrase.split())[:400]


# ---------- методы ----------
async def top(phrase: str, n: int = 200, regions=None, device: str | None = None, save: bool = True) -> dict:
    """Популярные запросы с фразой за 30 дней + похожие. save — сложить фразы в базу SEO-шника."""
    body = {"phrase": _clean(phrase), "numPhrases": str(max(1, min(n, 2000))), "devices": _devices(device)}
    if rg := _regions(regions):
        body["regions"] = rg
    d = await _post("topRequests", body)
    res = {"phrase": phrase, "total": _int(d.get("totalCount")),
           "results": [{"phrase": x.get("phrase", ""), "count": _int(x.get("count"))} for x in d.get("results", [])],
           "associations": [{"phrase": x.get("phrase", ""), "count": _int(x.get("count"))} for x in d.get("associations", [])]}
    if save:
        keywords.add_many([(x["phrase"], x["count"]) for x in res["results"] + res["associations"]], "wordstat-api")
    return res


def _month_end(d: dt.date) -> dt.date:
    return (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)


def _period_range(period: str, span: int | None) -> tuple[dt.date, dt.date]:
    today = dt.date.today()
    if period == "weekly":
        end = today - dt.timedelta(days=today.weekday() + 1)              # прошлое воскресенье
        return end - dt.timedelta(weeks=span or 26, days=-1), end
    if period == "daily":
        end = today - dt.timedelta(days=1)
        return end - dt.timedelta(days=(span or 60) - 1), end
    end = today.replace(day=1) - dt.timedelta(days=1)                      # конец прошлого месяца
    y, m = end.year, end.month - (span or 24) + 1
    while m <= 0:
        y, m = y - 1, m + 12
    return dt.date(y, m, 1), _month_end(end)


async def dynamics(phrase: str, period: str = "monthly", span: int | None = None, regions=None,
                   device: str | None = None) -> list[dict]:
    """Частота во времени. period: monthly (span месяцев, 24) | weekly (26 недель) | daily (60 дней)."""
    period = period if period in ("monthly", "weekly", "daily") else "monthly"
    d1, d2 = _period_range(period, span)
    body = {"phrase": _clean(phrase), "period": f"PERIOD_{period.upper()}",
            "fromDate": f"{d1:%Y-%m-%d}T00:00:00Z", "toDate": f"{d2:%Y-%m-%d}T23:59:59Z", "devices": _devices(device)}
    if rg := _regions(regions):
        body["regions"] = rg
    d = await _post("dynamics", body)
    return [{"date": str(x.get("date", ""))[:10], "count": _int(x.get("count")), "share": _float(x.get("share"))}
            for x in d.get("results", [])]


def trend(dyn: list[dict]) -> dict:
    """Сводка по месячной динамике: за последний месяц, к прошлому году, 3 мес. к 3 предыдущим, пиковый месяц."""
    c = [x["count"] for x in dyn]
    out = {"last": c[-1] if c else 0}
    if len(c) >= 13 and c[-13]:
        out["yoy"] = round((c[-1] / c[-13] - 1) * 100)
    if len(c) >= 6 and sum(c[-6:-3]):
        out["q"] = round((sum(c[-3:]) / sum(c[-6:-3]) - 1) * 100)
    if len(c) >= 12:
        months = {}
        for x in dyn[-24:]:
            months.setdefault(x["date"][5:7], []).append(x["count"])
        avg = {m: sum(v) / len(v) for m, v in months.items()}
        peak = max(avg, key=avg.get)
        mean = sum(avg.values()) / len(avg)
        if mean and avg[peak] > mean * 1.25:
            out["peak"] = MONTHS[int(peak) - 1]
    return out


MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def trend_text(t: dict) -> str:
    parts = []
    if "yoy" in t:
        parts.append(f"к прошлому году {t['yoy']:+d}%")
    if "q" in t:
        parts.append(f"3 мес. к 3 предыдущим {t['q']:+d}%")
    if "peak" in t:
        parts.append(f"пик — {t['peak']}")
    return ", ".join(parts) or "мало данных для тренда"


async def regions_tree() -> dict[str, str]:
    """{id: название} для всех регионов Вордстата."""
    d = await _post("getRegionsTree", {}, ttl=30 * 86400)
    out = {}

    def walk(items):
        for r in items or []:
            out[str(r.get("id"))] = r.get("label", "")
            walk(r.get("children"))
    walk(d.get("regions"))
    return out


async def regions(phrase: str, kind: str = "regions", device: str | None = None, limit: int = 15) -> list[dict]:
    """Где фразу ищут: kind regions (области) | cities (города) | all. Сортировка по числу запросов."""
    region = {"cities": "REGION_CITIES", "all": "REGION_ALL"}.get(kind, "REGION_REGIONS")
    d = await _post("regions", {"phrase": _clean(phrase), "region": region, "devices": _devices(device)})
    names = {}
    try:
        names = await regions_tree()
    except Exception as e:
        log.warning("дерево регионов: %s", e)
    rows = [{"region": names.get(str(x.get("region")), str(x.get("region"))), "count": _int(x.get("count")),
             "share": _float(x.get("share")), "affinity": round(_float(x.get("affinityIndex")))}
            for x in d.get("results", [])]
    rows.sort(key=lambda r: -r["count"])
    return rows[:limit]


async def compare(phrases: list[str], regions=None, device: str | None = None) -> list[dict]:
    """Сравнить фразы: частота за 30 дней, тренд по месяцам. По убыванию частоты."""
    out = []
    for p in [p.strip() for p in phrases if p.strip()][:15]:
        row = {"phrase": p}
        try:
            row["total"] = (await top(p, 1, regions, device, save=False))["total"]
            row.update(trend(await dynamics(p, "monthly", 24, regions, device)))
        except Exception as e:
            row["error"] = str(e)[:150]
        out.append(row)
    out.sort(key=lambda r: -r.get("total", 0))
    return out


async def parse(seed: str, n: int = 1000, depth: int = 1, regions=None) -> dict:
    """Собрать семантику по фразе: топ + похожие; depth=2 — ещё по 10 самым частым фразам из результата.
    Всё ложится в базу keywords (source wordstat-api). Вернёт сколько фраз собрано и главные из них."""
    seen: dict[str, int] = {}
    first = await top(seed, n, regions)
    for x in first["results"] + first["associations"]:
        seen[x["phrase"]] = max(seen.get(x["phrase"], 0), x["count"])
    if depth >= 2:
        nxt = [x["phrase"] for x in sorted(first["results"][1:] + first["associations"], key=lambda x: -x["count"])[:10]]
        for p in nxt:
            try:
                r = await top(p, min(n, 300), regions)
            except Exception as e:
                log.warning("парсинг %s: %s", p, e)
                continue
            for x in r["results"] + r["associations"]:
                seen[x["phrase"]] = max(seen.get(x["phrase"], 0), x["count"])
    best = sorted(seen.items(), key=lambda kv: -kv[1])
    return {"seed": seed, "total": first["total"], "collected": len(seen),
            "top": [{"phrase": p, "count": c} for p, c in best[:30]]}


async def refresh_seeds(force: bool = False) -> str:
    """Раз в неделю перед контент-планом: обновить семантику по WORDSTAT_SEEDS (не чаще раза в 6 дней)."""
    if not enabled() or not config.WORDSTAT_SEEDS:
        return ""
    if not force and time.time() - (store.get("ws_seeds_ts") or 0) < 6 * 86400:
        return ""
    n = 0
    for s in config.WORDSTAT_SEEDS:
        try:
            n += (await parse(s, 500))["collected"]
        except Exception as e:
            log.warning("сид %s: %s", s, e)
    store.put("ws_seeds_ts", time.time())
    return f"Вордстат API: обновил семантику по {len(config.WORDSTAT_SEEDS)} базовым фразам, фраз: {n}"


# ---------- текст для бота и агента ----------
def _n(x: int) -> str:
    return f"{x:,}".replace(",", " ")


def fmt_top(r: dict, k: int = 20) -> str:
    L = [f"«{r['phrase']}» — {r['total']:,} запросов за 30 дней".replace(",", " ")]
    if r["results"]:
        L.append("С этой фразой ищут:")
        L += [f"  {x['count']:>8,}  {x['phrase']}".replace(",", " ") for x in r["results"][:k]]
    if r["associations"]:
        L.append("Похожие запросы:")
        L += [f"  {x['count']:>8,}  {x['phrase']}".replace(",", " ") for x in r["associations"][:k // 2]]
    return "\n".join(L)


def fmt_dynamics(dyn: list[dict], period: str = "monthly") -> str:
    if not dyn:
        return "динамики нет"
    mx = max(x["count"] for x in dyn) or 1
    rows = dyn[-12:] if period == "monthly" else dyn[-14:]
    L = [f"  {x['date'][:7] if period == 'monthly' else x['date']}  {x['count']:>8,}  {'▇' * max(1, round(x['count'] / mx * 12))}"
         .replace(",", " ") for x in rows]
    if period == "monthly":
        L.append("  " + trend_text(trend(dyn)))
    return "\n".join(L)


def fmt_regions(rows: list[dict]) -> str:
    return "\n".join(f"  {r['region']}: {r['count']:,} · индекс {r['affinity']}".replace(",", " ") for r in rows) or "нет данных"


def fmt_compare(rows: list[dict]) -> str:
    L = []
    for r in rows:
        if "error" in r:
            L.append(f"«{r['phrase']}» — ошибка: {r['error']}")
            continue
        L.append(f"«{r['phrase']}» — {_n(r.get('total', 0))}/30 дн. · {trend_text(r)}")
    return "\n".join(L)
