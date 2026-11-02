"""Яндекс Вебмастер, Яндекс Метрика, IndexNow.

Всё работает только когда в .env заполнены токены; без них функции тихо возвращают None,
а мониторинг продолжает делать проверки, которым токены не нужны.

YANDEX_OAUTH_TOKEN   — OAuth-токен Яндекса с правами на Вебмастер и Метрику
                       (приложение на oauth.yandex.ru: права webmaster:hostinfo, webmaster:verify, metrika:read)
METRIKA_COUNTER_ID   — номер счётчика Метрики dsgners.ru
INDEXNOW_KEY         — ключ IndexNow; файл <ключ>.txt с этим же ключом внутри должен лежать в корне сайта
"""
import datetime as dt
import logging
from urllib.parse import quote, urlparse

import aiohttp

from core import config

WM = "https://api.webmaster.yandex.net/v4"
MK = "https://api-metrika.yandex.net/stat/v1/data"
log = logging.getLogger("seo.apis")


def has_yandex() -> bool:
    return bool(config.YANDEX_OAUTH_TOKEN)


def _h():
    return {"Authorization": f"OAuth {config.YANDEX_OAUTH_TOKEN}"}


async def _get(url, params=None):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
        async with s.get(url, headers=_h(), params=params) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {str(data)[:300]}")
            return data


async def _post(url, payload):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
        async with s.post(url, headers=_h(), json=payload) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}: {str(data)[:300]}")
            return data


# ---------- Вебмастер ----------
_host_cache: dict = {}


async def wm_host() -> tuple[str, str] | None:
    """(user_id, host_id) для SEO_SITE или None."""
    if not has_yandex():
        return None
    if "h" in _host_cache:
        return _host_cache["h"]
    uid = (await _get(f"{WM}/user"))["user_id"]
    hosts = (await _get(f"{WM}/user/{uid}/hosts")).get("hosts", [])
    site = config.SEO_SITE.lower()
    for h in hosts:
        if urlparse(h.get("unicode_host_url") or h.get("ascii_host_url", "")).netloc.lower().removeprefix("www.") == site:
            _host_cache["h"] = (str(uid), h["host_id"])
            return _host_cache["h"]
    raise RuntimeError(f"Сайт {site} не найден в Вебмастере этого аккаунта")


async def wm_summary() -> dict | None:
    """В поиске / исключено / ИКС / проблемы по уровням."""
    h = await wm_host()
    if not h:
        return None
    uid, host = h
    return await _get(f"{WM}/user/{uid}/hosts/{quote(host, safe='')}/summary")


async def wm_diagnostics() -> list[dict] | None:
    """Актуальные проблемы сайта из «Диагностики»: [{code, severity}]."""
    h = await wm_host()
    if not h:
        return None
    uid, host = h
    data = await _get(f"{WM}/user/{uid}/hosts/{quote(host, safe='')}/diagnostics")
    out = []
    for code, p in (data.get("problems") or {}).items():
        if p.get("state") == "PRESENT":
            out.append({"code": code, "severity": p.get("severity", "")})
    return out


async def wm_queries(days=7) -> list[dict] | None:
    """Популярные запросы за период: показы, клики, средняя позиция."""
    h = await wm_host()
    if not h:
        return None
    uid, host = h
    end = dt.date.today() - dt.timedelta(days=2)   # данные Вебмастера приходят с задержкой
    start = end - dt.timedelta(days=days - 1)
    params = [("order_by", "TOTAL_SHOWS"), ("query_indicator", "TOTAL_SHOWS"), ("query_indicator", "TOTAL_CLICKS"),
              ("query_indicator", "AVG_SHOW_POSITION"), ("date_from", start.isoformat()), ("date_to", end.isoformat()),
              ("limit", "500")]
    data = await _get(f"{WM}/user/{uid}/hosts/{quote(host, safe='')}/search-queries/popular", params)
    out = []
    for q in data.get("queries", []):
        ind = q.get("indicators", {})
        out.append({"query": q.get("query_text", ""), "shows": ind.get("TOTAL_SHOWS", 0),
                    "clicks": ind.get("TOTAL_CLICKS", 0), "pos": ind.get("AVG_SHOW_POSITION", 0)})
    return out


async def wm_recrawl(urls: list[str]) -> dict:
    """Отправить адреса на переобход. Квота у Яндекса суточная — проверяем остаток."""
    h = await wm_host()
    if not h or not urls:
        return {"sent": 0, "note": "нет токена" if not h else ""}
    uid, host = h
    base = f"{WM}/user/{uid}/hosts/{quote(host, safe='')}/recrawl"
    try:
        left = (await _get(f"{base}/quota")).get("quota_remainder", len(urls))
    except Exception:
        left = len(urls)
    sent, errs = 0, []
    for u in urls[: max(0, int(left))]:
        try:
            await _post(f"{base}/queue", {"url": u})
            sent += 1
        except Exception as e:
            errs.append(f"{u}: {e}")
    return {"sent": sent, "left": int(left) - sent, "errors": errs[:5]}


# ---------- Метрика ----------
async def organic_by_page(date1: dt.date, date2: dt.date, limit=1000) -> dict[str, int] | None:
    """Визиты из поиска по страницам входа: {адрес: визиты}."""
    if not (has_yandex() and config.METRIKA_COUNTER_ID):
        return None
    params = {"ids": config.METRIKA_COUNTER_ID, "metrics": "ym:s:visits", "dimensions": "ym:s:startURL",
              "filters": "ym:s:lastTrafficSource=='organic'", "date1": date1.isoformat(),
              "date2": date2.isoformat(), "limit": str(limit), "sort": "-ym:s:visits"}
    data = await _get(MK, params)
    out = {}
    for row in data.get("data", []):
        url = (row.get("dimensions") or [{}])[0].get("name") or ""
        out[url] = int((row.get("metrics") or [0])[0])
    return out


async def organic_total(date1: dt.date, date2: dt.date) -> int | None:
    if not (has_yandex() and config.METRIKA_COUNTER_ID):
        return None
    params = {"ids": config.METRIKA_COUNTER_ID, "metrics": "ym:s:visits",
              "filters": "ym:s:lastTrafficSource=='organic'", "date1": date1.isoformat(), "date2": date2.isoformat()}
    data = await _get(MK, params)
    return int((data.get("totals") or [0])[0])


# ---------- IndexNow (Яндекс и Bing сразу узнают о новой странице) ----------
async def indexnow(urls: list[str]) -> str:
    key = config.INDEXNOW_KEY
    if not key or not urls:
        return "нет ключа INDEXNOW_KEY" if not key else "нечего отправлять"
    host = config.SEO_SITE
    payload = {"host": host, "key": key, "keyLocation": f"https://{host}/{key}.txt", "urlList": urls[:10000]}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        async with s.post("https://yandex.com/indexnow", json=payload) as r:
            return f"HTTP {r.status}"
