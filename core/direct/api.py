"""Яндекс Директ API v5 + Метрика по трафику из Директа.

DIRECT_TOKEN         — OAuth-токен с доступом к API Директа (приложение на oauth.yandex.ru с правом direct:api,
                       заявка на доступ к API в интерфейсе Директа: Настройки → API). Пусто — берём YANDEX_OAUTH_TOKEN.
DIRECT_CLIENT_LOGIN  — логин клиента, только если аккаунт агентский
DIRECT_SANDBOX=1     — песочница Директа (тестовые данные), пока заявка на API не одобрена

Отчёты (Reports) строятся в очереди: 201/202 — «ещё готовится», ждём retryIn и спрашиваем снова.
Деньги в отчётах — в валюте аккаунта (returnMoneyInMicros: false), в сервисах — в микроединицах (÷ 1 000 000).
"""
import asyncio
import datetime as dt
import hashlib
import json
import logging
import time

import aiohttp

from core import config

log = logging.getLogger("direct.api")

LIVE = "https://api.direct.yandex.com/json/v5"
SANDBOX = "https://api-sandbox.direct.yandex.com/json/v5"
MK = "https://api-metrika.yandex.net/stat/v1/data"
MICRO = 1_000_000
_units = {"last": ""}       # остаток баллов API из заголовка Units — для /direct_status


class DirectError(RuntimeError):
    pass


def token() -> str:
    return config.env("DIRECT_TOKEN") or config.YANDEX_OAUTH_TOKEN


def sandbox() -> bool:
    return config.env("DIRECT_SANDBOX", "0") == "1"


def enabled() -> bool:
    return bool(token())


def base() -> str:
    return SANDBOX if sandbox() else LIVE


def units() -> str:
    return _units["last"]


def _headers(extra: dict | None = None) -> dict:
    h = {"Authorization": f"Bearer {token()}", "Accept-Language": "ru"}
    login = config.env("DIRECT_CLIENT_LOGIN")
    if login:
        h["Client-Login"] = login
    if extra:
        h.update(extra)
    return h


async def call(service: str, method: str, params: dict) -> dict:
    """Вызов сервиса Директа (campaigns, ads …). Вернёт result или бросит DirectError."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.post(f"{base()}/{service}", headers=_headers(),
                          json={"method": method, "params": params}) as r:
            if r.headers.get("Units"):
                _units["last"] = r.headers["Units"]
            data = await r.json(content_type=None)
    if not isinstance(data, dict):
        raise DirectError(f"{service}.{method}: странный ответ {str(data)[:200]}")
    if "error" in data:
        e = data["error"]
        raise DirectError(f"{service}.{method}: {e.get('error_code')} {e.get('error_string')} — {e.get('error_detail')}")
    return data.get("result") or {}


async def get_all(service: str, params: dict, key: str, limit: int = 10000) -> list[dict]:
    """get с постраничной выдачей (LimitedBy)."""
    out, offset = [], 0
    while True:
        p = dict(params, Page={"Limit": limit, "Offset": offset})
        res = await call(service, "get", p)
        out += res.get(key) or []
        if not res.get("LimitedBy"):
            return out
        offset = res["LimitedBy"]


# ---------- отчёты ----------

def _num(v: str):
    if v in ("--", ""):
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in v else f
    except ValueError:
        return v


def parse_tsv(text: str) -> list[dict]:
    """TSV отчёта без шапки и итогов → список строк-словарей. «--» → None, числа → числа (Id остаются числами)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    head = lines[0].split("\t")
    rows = []
    for ln in lines[1:]:
        cells = ln.split("\t")
        if len(cells) != len(head):
            continue
        rows.append({h: (_num(c) if h not in ("Placement", "CampaignName", "Date") else (None if c == "--" else c))
                     for h, c in zip(head, cells)})
    return rows


async def report(fields: list[str], date_from: dt.date, date_to: dt.date, filters: list[dict] | None = None,
                 fresh: bool = False, max_wait: int = 600) -> list[dict]:
    """Пользовательский отчёт (CUSTOM_REPORT) за даты. fresh=True — новое имя отчёта (данные за сегодня меняются)."""
    sel = {"DateFrom": date_from.isoformat(), "DateTo": date_to.isoformat()}
    if filters:
        sel["Filter"] = filters
    params = {"SelectionCriteria": sel, "FieldNames": fields, "ReportType": "CUSTOM_REPORT",
              "DateRangeType": "CUSTOM_DATE", "Format": "TSV", "IncludeVAT": "YES"}
    key = json.dumps(params, sort_keys=True) + (str(int(time.time() // 600)) if fresh else "")
    params["ReportName"] = "dsg-" + hashlib.md5(key.encode()).hexdigest()[:16]
    hdr = _headers({"processingMode": "auto", "returnMoneyInMicros": "false",
                    "skipReportHeader": "true", "skipReportSummary": "true"})
    t0 = time.time()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as s:
        while True:
            async with s.post(f"{base()}/reports", headers=hdr, json={"params": params}) as r:
                if r.headers.get("Units"):
                    _units["last"] = r.headers["Units"]
                text = await r.text()
                if r.status == 200:
                    return parse_tsv(text)
                if r.status in (201, 202):
                    wait = int(r.headers.get("retryIn") or 5)
                    if time.time() - t0 + wait > max_wait:
                        raise DirectError(f"отчёт не готов за {max_wait} с")
                    await asyncio.sleep(min(max(wait, 2), 60))
                    continue
                try:
                    e = json.loads(text).get("error", {})
                    msg = f"{e.get('error_code')} {e.get('error_string')} — {e.get('error_detail')}"
                except ValueError:
                    msg = text[:300]
                raise DirectError(f"отчёт: HTTP {r.status}: {msg}")


# ---------- сервисы ----------

CAMPAIGN_FIELDS = ["Id", "Name", "Type", "State", "Status", "StatusClarification", "DailyBudget"]


async def campaigns(with_excluded: bool = False) -> list[dict]:
    fields = CAMPAIGN_FIELDS + (["ExcludedSites"] if with_excluded else [])
    return await get_all("campaigns", {"SelectionCriteria": {}, "FieldNames": fields}, "Campaigns")


async def ads(campaign_ids: list[int]) -> list[dict]:
    """Объявления с адресом (Href). Разные типы объявлений хранят ссылку в разных блоках."""
    out = []
    for i in range(0, len(campaign_ids), 10):          # не больше 10 кампаний в одном запросе
        base_p = {"SelectionCriteria": {"CampaignIds": campaign_ids[i:i + 10]},
                  "FieldNames": ["Id", "CampaignId", "AdGroupId", "State", "Status", "StatusClarification", "Type"],
                  "TextAdFieldNames": ["Href", "Title", "Text"]}
        try:
            out += await get_all("ads", dict(base_p, TextImageAdFieldNames=["Href"]), "Ads")
        except DirectError as e:
            log.warning("ads с картинками не прочитались (%s) — беру только текстовые", e)
            out += await get_all("ads", base_p, "Ads")
    return out


def href_of(ad: dict) -> str:
    for k in ("TextAd", "TextImageAd"):
        h = (ad.get(k) or {}).get("Href")
        if h:
            return h
    return ""


def title_of(ad: dict) -> str:
    return (ad.get("TextAd") or {}).get("Title") or ""


async def add_excluded(campaign_id: int, sites: list[str], limit: int = 1000) -> tuple[list[str], str]:
    """Дописать площадки в «Запрещённые площадки» кампании. Вернёт (добавленные, примечание)."""
    res = await call("campaigns", "get", {"SelectionCriteria": {"Ids": [campaign_id]},
                                          "FieldNames": ["Id", "ExcludedSites"]})
    cur = ((res.get("Campaigns") or [{}])[0].get("ExcludedSites") or {}).get("Items") or []
    have = {x.lower() for x in cur}
    add = [s for s in sites if s.lower() not in have]
    note = ""
    if len(cur) + len(add) > limit:
        add = add[:max(0, limit - len(cur))]
        note = f"упёрлись в лимит {limit} площадок на кампанию"
    if not add:
        return [], note or "уже были в списке"
    up = await call("campaigns", "update", {"Campaigns": [{"Id": campaign_id,
                                                           "ExcludedSites": {"Items": cur + add}}]})
    r0 = (up.get("UpdateResults") or [{}])[0]
    if r0.get("Errors"):
        raise DirectError("; ".join(f"{e.get('Message')} {e.get('Details', '')}" for e in r0["Errors"]))
    if r0.get("Warnings"):
        note = (note + "; " if note else "") + "; ".join(w.get("Message", "") for w in r0["Warnings"])
    return add, note


# ---------- Метрика: трафик из Директа ----------

FROM_DIRECT = "ym:s:lastTrafficSource=='ad' AND ym:s:lastAdvEngine=='ya_direct'"


def has_metrika() -> bool:
    return bool(config.YANDEX_OAUTH_TOKEN and config.METRIKA_COUNTER_ID)


def goals() -> list[tuple[str, str]]:
    """DIRECT_GOALS=read_90:123,signup_open:456,signup_done:789 — цели воронки по порядку: (название, id цели)."""
    out = []
    for part in config.env("DIRECT_GOALS").split(","):
        name, _, gid = part.strip().partition(":")
        if name and gid.strip().isdigit():
            out.append((name.strip(), gid.strip()))
    return out


async def _mk(params: dict) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.get(MK, headers={"Authorization": f"OAuth {config.YANDEX_OAUTH_TOKEN}"}, params=params) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise RuntimeError(f"Метрика HTTP {r.status}: {str(data)[:300]}")
            return data


def _metrics() -> list[str]:
    base_m = ["ym:s:visits", "ym:s:bounceRate", "ym:s:pageDepth", "ym:s:avgVisitDurationSeconds"]
    return base_m + [f"ym:s:goal{gid}visits" for _, gid in goals()[:16]]


def _row(names: list[str], vals: list) -> dict:
    out = {"visits": vals[0] or 0, "bounce": vals[1] or 0, "depth": vals[2] or 0, "duration": vals[3] or 0}
    for (name, _), v in zip(goals()[:16], vals[4:]):
        out[name] = v or 0
    return out


async def metrika_by(dimension: str, d1: dt.date, d2: dt.date, limit: int = 500) -> dict[str, dict] | None:
    """Визиты из Директа по измерению (ym:s:startURL, ym:s:deviceCategory …): {значение: метрики}."""
    if not has_metrika():
        return None
    m = _metrics()
    data = await _mk({"ids": config.METRIKA_COUNTER_ID, "metrics": ",".join(m), "dimensions": dimension,
                      "filters": FROM_DIRECT, "date1": d1.isoformat(), "date2": d2.isoformat(),
                      "limit": str(limit), "sort": "-ym:s:visits", "accuracy": "full"})
    out = {}
    for row in data.get("data", []):
        key = (row.get("dimensions") or [{}])[0].get("name") or ""
        out[key] = _row(m, row.get("metrics") or [])
    return out


async def metrika_total(d1: dt.date, d2: dt.date) -> dict | None:
    if not has_metrika():
        return None
    m = _metrics()
    data = await _mk({"ids": config.METRIKA_COUNTER_ID, "metrics": ",".join(m), "filters": FROM_DIRECT,
                      "date1": d1.isoformat(), "date2": d2.isoformat(), "accuracy": "full"})
    return _row(m, data.get("totals") or [0] * len(m))
