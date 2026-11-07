"""Работа Директ-агента: сбор, сводки, отчёты, площадки, воронка и расписание (время — SEO_TZ, Москва).

каждый час     — кампании и объявления: остановки, отклонения модерации; расход за сегодня к потолку DIRECT_DAILY_CAP
09:00          — сбор за 3 дня (Метрика досчитывает прошлые дни) → сводка за вчера + мусорные площадки на кнопку
понедельник 10:15 — недельный отчёт: неделя к неделе, кампании, устройства, статьи, площадки, воронка
Первый запуск без истории — догружает DIRECT_BACKFILL_DAYS дней.
Этап 1: агент только читает. Изменение в Директе одно — исключение площадок, и только по кнопке владельца.
"""
import asyncio
import datetime as dt
import logging
import re
import time
from zoneinfo import ZoneInfo

from core import config
from core.direct import api, store

log = logging.getLogger("direct.jobs")

ENABLED = config.env("DIRECT_ENABLED", "1") == "1"
DAILY_CAP = float(config.env("DIRECT_DAILY_CAP", "0") or 0)          # ₽ в день на аккаунт; 0 — не следим
REPORT_HOUR = int(config.env("DIRECT_REPORT_HOUR", "9"))
BACKFILL = int(config.env("DIRECT_BACKFILL_DAYS", "30"))
JUNK_MIN_CLICKS = int(config.env("DIRECT_JUNK_MIN_CLICKS", "30"))
JUNK_BOUNCE = float(config.env("DIRECT_JUNK_BOUNCE", "70"))          # % отказов, выше — мусор
JUNK_LANDING = float(config.env("DIRECT_JUNK_LANDING", "0.5"))       # визитов на клик, ниже — недолёт/боты
JUNK_DAYS = int(config.env("DIRECT_JUNK_DAYS", "14"))
ART_MIN_CLICKS = int(config.env("DIRECT_ARTICLE_MIN_CLICKS", "30"))  # меньше кликов — «рано судить»
# Площадки Яндекса не исключаем: их нельзя запретить или это поиск
OWN = re.compile(r"(?i)яндекс|yandex|^ya\.ru$|^—$|^$")

AD_FIELDS = ["Date", "CampaignId", "AdId", "AdNetworkType", "Device", "Impressions", "Clicks", "Cost", "Sessions", "Bounces"]
PLACE_FIELDS = ["Date", "CampaignId", "Placement", "AdNetworkType", "Impressions", "Clicks", "Cost", "Sessions", "Bounces"]
NET = {"AD_NETWORK": "РСЯ", "SEARCH": "Поиск"}
DEV = {"DESKTOP": "Десктоп", "MOBILE": "Мобильные", "TABLET": "Планшеты"}

_send = None      # async (text, buttons: list[list[(текст, data)]] | None)
_running: set[str] = set()


def setup(send):
    global _send
    _send = send


async def say(text: str, buttons=None):
    if _send:
        try:
            await _send(text, buttons)
        except Exception:
            log.exception("direct notify failed")


# ---------- мелочи ----------

def _now() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(config.SEO_TZ))


def _today() -> dt.date:
    return _now().date()


def _iso(d: dt.date) -> str:
    return d.isoformat()


def rub(x: float | None) -> str:
    if x is None:
        return "—"
    if abs(x) >= 100:
        return f"{x:,.0f}".replace(",", " ") + " ₽"
    return f"{x:.1f}".replace(".", ",").removesuffix(",0") + " ₽"


def pct(a: float, b: float) -> str:
    """Изменение a к b."""
    if not b:
        return ""
    d = (a - b) / b * 100
    return f" ({'+' if d >= 0 else '−'}{abs(d):.0f}%)"


def quality(r: dict) -> dict:
    """Качественный визит = визит без отказа (Метрика: отказ — одна страница и меньше 15 с)."""
    clicks, cost = r.get("clicks") or 0, r.get("cost") or 0.0
    sessions, bounces = r.get("sessions") or 0, r.get("bounces") or 0
    q = max(0, sessions - bounces)
    return dict(r, clicks=clicks, cost=cost, sessions=sessions, bounces=bounces, quality=q,
                cpc=cost / clicks if clicks else None, cpq=cost / q if q else None,
                bounce_rate=bounces / sessions * 100 if sessions else None,
                landing=sessions / clicks if clicks else None)


def junk(rows: list[dict], min_clicks: int = JUNK_MIN_CLICKS, bounce_thr: float = JUNK_BOUNCE,
         landing_thr: float = JUNK_LANDING) -> list[dict]:
    """Мусорные площадки РСЯ: достаточно кликов и много отказов или клики не доходят до сайта. Дорогие — первыми."""
    out = []
    for r in rows:
        r = quality(r)
        if r["clicks"] < min_clicks or OWN.search(r.get("placement") or ""):
            continue
        why = []
        if r["bounce_rate"] is not None and r["bounce_rate"] >= bounce_thr:
            why.append(f"отказы {r['bounce_rate']:.0f}%")
        if r["landing"] is not None and r["landing"] < landing_thr:
            why.append(f"до сайта доходит {r['landing'] * 100:.0f}% кликов")
        if why:
            out.append(dict(r, why=", ".join(why)))
    return sorted(out, key=lambda x: -x["cost"])


def _period(days: int, end: dt.date | None = None) -> tuple[str, str]:
    end = end or _today() - dt.timedelta(days=1)
    return _iso(end - dt.timedelta(days=days - 1)), _iso(end)


def _one(rows: list[dict]) -> dict:
    return quality(rows[0] if rows and rows[0].get("clicks") is not None else {})


def _title(url: str, fallback: str = "") -> str:
    try:
        from core.seo import store as seo_store
        r = seo_store.db().execute("SELECT title FROM urls WHERE url IN (?,?,?) AND title!='' LIMIT 1",
                                   (url, url + "/", url.replace("https://", "http://"))).fetchone()
        if r:
            return re.sub(r"\s*[—|-]\s*дизайнерс\s*$", "", r[0]).strip()
    except Exception:
        pass
    return fallback or url.split("/")[-1][:60]


def _metrika_by_article(m: dict | None) -> dict[str, dict]:
    if not m:
        return {}
    out: dict[str, dict] = {}
    for url, v in m.items():
        a = store.article_of(url)
        if a not in out:
            out[a] = dict(v)
            continue
        cur, n0, n1 = out[a], out[a]["visits"], v["visits"]
        for k in ("bounce", "depth", "duration"):          # средние — взвешиваем по визитам
            cur[k] = (cur[k] * n0 + v[k] * n1) / (n0 + n1) if n0 + n1 else 0
        for k, x in v.items():
            if k not in ("bounce", "depth", "duration"):
                cur[k] = cur.get(k, 0) + x
    return out


# ---------- сбор ----------

async def collect(days: int = 3) -> tuple[list, list, str]:
    """Кампании, объявления и статистика за последние дни (по вчера). Вернёт (изменения кампаний, отклонённые, заметка)."""
    camps = await api.campaigns()
    changes = store.save_campaigns(camps)
    ids = [c["Id"] for c in camps]
    rejected = store.save_ads(await api.ads(ids), api.href_of, api.title_of) if ids else []
    if not store.has_stats():
        days = max(days, BACKFILL)
    end = _today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    daylist = [_iso(start + dt.timedelta(days=i)) for i in range(days)]
    store.save_ad_stats(await api.report(AD_FIELDS, start, end), daylist)
    store.save_place_stats(await api.report(PLACE_FIELDS, start, end), daylist)
    store.put("collected", {"ts": time.time(), "from": daylist[0], "to": daylist[-1]})
    return changes, rejected, f"кампаний {len(camps)}, дни {daylist[0]} — {daylist[-1]}"


async def _alert_changes(changes: list, rejected: list):
    for c, what in changes:
        await say(f"⚠️ Директ · «{c['name']}»: {what}" + (f"\n{c['note']}" if c.get("note") else ""))
    if rejected:
        lines = [f"∙ «{store.campaign_name(a['campaign_id'])}» · {a['title'] or a['id']}"
                 + (f" — {a['note']}" if a.get("note") else "") for a in rejected[:10]]
        await say(f"⚠️ Директ: модерация отклонила объявлений — {len(rejected)}\n" + "\n".join(lines))


# ---------- каждый час ----------

async def hourly() -> str:
    camps = await api.campaigns()
    changes = store.save_campaigns(camps)
    ids = [c["Id"] for c in camps]
    rejected = store.save_ads(await api.ads(ids), api.href_of, api.title_of) if ids else []
    await _alert_changes(changes, rejected)
    today = _today()
    rows = await api.report(["CampaignId", "Clicks", "Cost"], today, today, fresh=True)
    cost = sum(float(r.get("Cost") or 0) for r in rows)
    clicks = sum(int(r.get("Clicks") or 0) for r in rows)
    note = f"сегодня {rub(cost)}, кликов {clicks}"
    key = f"cap:{_iso(today)}"
    sent = store.get(key, [])
    if DAILY_CAP > 0:
        for lvl, text in ((100, "🛑 Директ: дневной потолок пройден"), (80, "⚠️ Директ: потрачено 80% дневного потолка")):
            if cost >= DAILY_CAP * lvl / 100 and lvl not in sent:
                await say(f"{text} — {rub(cost)} из {rub(DAILY_CAP)}. Сам агент ничего не останавливает: "
                          "выключить рекламу — в кабинете Директа.")
                store.put(key, sent + [lvl])
                break
    # показы встали: после полудня ноль кликов, хотя вчера были и есть включённые кампании
    on = [c for c in camps if c.get("State") == "ON"]
    y = store.totals(*_period(1))
    if (_now().hour >= 12 and on and clicks == 0 and (y and (y[0].get("clicks") or 0) > 20)
            and not store.get(f"zero:{_iso(today)}")):
        store.put(f"zero:{_iso(today)}", True)
        await say(f"⚠️ Директ: сегодня с утра ни одного клика, вчера было {y[0]['clicks']}. "
                  "Проверь баланс, модерацию и ставки в кабинете.")
    return note


# ---------- сводка за вчера ----------

def _line(name: str, r: dict) -> str:
    r = quality(r)
    return (f"{name}: {rub(r['cost'])} · кликов {r['clicks']} · CPC {rub(r['cpc'])} · "
            f"качественных визитов {r['quality']} · за визит {rub(r['cpq'])}")


def _art_line(r: dict, met: dict | None = None) -> str:
    r = quality(r)
    s = f"«{_title(r['article'], r.get('ad_title') or '')}» — {rub(r['cpq'])} за визит, отказы {r['bounce_rate'] or 0:.0f}%, {r['clicks']} кл."
    if met:
        s += f", глубина {met['depth']:.1f}, {met['duration'] / 60:.1f} мин"
        last = api.goals()[-1][0] if api.goals() else ""
        if last and met.get(last):
            s += f", {last}: {int(met[last])}"
    return s


def _ranked_articles(d1: str, d2: str) -> list[dict]:
    rows = [quality(r) for r in store.by_article(d1, d2)]
    return sorted([r for r in rows if r["clicks"] >= ART_MIN_CLICKS and r["cpq"] is not None], key=lambda r: r["cpq"])


def propose_places() -> tuple[list[dict], int]:
    """Мусорные площадки за JUNK_DAYS дней, которых ещё нет в исключённых и «не трогать». Сохраняет пакет-предложение."""
    rows = junk(store.by_placement(*_period(JUNK_DAYS)))
    rows = [r for r in rows if store.place_status(r["placement"]) not in ("excluded", "keep")]
    for r in rows:
        store.set_place(r["placement"], "proposed", r["why"])
    batch = int(time.time())
    store.put("proposal", {"id": batch, "items": [r["placement"] for r in rows]})
    return rows, batch


def places_text(rows: list[dict], limit: int = 25) -> str:
    if not rows:
        return "Мусорных площадок за две недели не нашёл."
    wasted = sum(r["cost"] for r in rows)
    lines = [f"{i}. {r['placement']} — {rub(r['cost'])}, {r['clicks']} кл., {r['why']}" for i, r in enumerate(rows[:limit], 1)]
    more = f"\n… и ещё {len(rows) - limit}" if len(rows) > limit else ""
    return (f"🗑 Мусорные площадки РСЯ за {JUNK_DAYS} дн.: {len(rows)}, на них ушло {rub(wasted)}\n"
            + "\n".join(lines) + more
            + "\n\nОставить какие-то: /direct_keep 2,5 · исключить только часть: /direct_exclude 1,3")


def place_buttons(rows: list[dict], batch: int):
    if not rows:
        return None
    return [[(f"🚫 Исключить все ({len(rows)})", f"dr:xa:{batch}"), ("Не трогать эти", f"dr:ka:{batch}")]]


async def daily() -> str:
    changes, rejected, note = await collect(3)
    await _alert_changes(changes, rejected)
    y1, y2 = _period(1)
    p1, p2 = _period(7, _today() - dt.timedelta(days=2))
    yday = _one(store.totals(y1, y2))
    prev = _one(store.totals(p1, p2))
    avg = {k: (prev.get(k) or 0) / 7 for k in ("cost", "clicks", "quality")}
    lines = [f"📊 Директ — вчера, {dt.date.fromisoformat(y1):%d.%m}",
             f"Расход {rub(yday['cost'])}{pct(yday['cost'], avg['cost'])}"
             + (f" из потолка {rub(DAILY_CAP)}" if DAILY_CAP else "")
             + f" · кликов {yday['clicks']}{pct(yday['clicks'], avg['clicks'])} · CPC {rub(yday['cpc'])}",
             f"Качественных визитов {yday['quality']}{pct(yday['quality'], avg['quality'])}"
             f" · за визит {rub(yday['cpq'])} · отказы {yday['bounce_rate'] or 0:.0f}%",
             "(в скобках — к среднему дню прошлой недели)"]
    nets = store.totals(y1, y2, "network")
    if len(nets) > 1:
        lines.append("")
        lines += [_line(NET.get(n["network"], n["network"]), n) for n in nets if n.get("clicks")]
    if yday["clicks"] > 50 and not yday["sessions"]:
        lines.append("\n⚠️ Визитов из Метрики нет — похоже, счётчик не привязан к кампаниям "
                     "(в настройках кампании → «Счётчики Метрики»). Без него не видно отказов и качества.")
    arts = _ranked_articles(*_period(7))
    if len(arts) >= 5:
        lines.append("\nСтатьи за 7 дней (цена качественного визита):")
        lines += [f"👍 {_art_line(r)}" for r in arts[:3]]
        lines += [f"👎 {_art_line(r)}" for r in arts[-2:]]
    elif arts:
        lines.append("\nСтатьи за 7 дней, от дешёвого качественного визита к дорогому:")
        lines += [f"{i}. {_art_line(r)}" for i, r in enumerate(arts, 1)]
    await say("\n".join(lines))
    rows, batch = propose_places()
    if rows:
        await say(places_text(rows, 10), place_buttons(rows, batch))
    return f"{note}; вчера {rub(yday['cost'])}, мусорных площадок {len(rows)}"


# ---------- неделя ----------

async def funnel_text(d1: dt.date, d2: dt.date) -> str:
    clicks = _one(store.totals(_iso(d1), _iso(d2)))["clicks"]
    if not api.has_metrika():
        return "Воронка: нет доступа к Метрике (YANDEX_OAUTH_TOKEN, METRIKA_COUNTER_ID)."
    tot = await api.metrika_total(d1, d2)
    if not tot or not tot["visits"]:
        return "Воронка: Метрика не видит визитов из Директа за эти дни."
    stages = [("Клики", clicks), ("Визиты", tot["visits"]), ("Без отказа", round(tot["visits"] * (1 - tot["bounce"] / 100)))]
    stages += [(name, int(tot.get(name) or 0)) for name, _ in api.goals()]
    lines, worst = [f"🔻 Воронка {d1:%d.%m}–{d2:%d.%m}"], None
    for i, (name, n) in enumerate(stages):
        conv = ""
        if i and stages[i - 1][1]:
            share = n / stages[i - 1][1] * 100
            conv = f" — {share:.0f}% от прошлого шага"
            if i > 1 and (worst is None or share < worst[1]):
                worst = (name, share)
        lines.append(f"{i + 1}. {name}: {n}{conv}")
    if not api.goals():
        lines.append("\nДальше «без отказа» воронку не вижу: нужны цели в Метрике (дочитал, вторая статья, "
                     "открыл и завершил регистрацию) и строка DIRECT_GOALS в настройках.")
    elif worst:
        lines.append(f"\nСамый большой провал — на шаге «{worst[0]}»: доходит {worst[1]:.0f}%.")
    devs = await api.metrika_by("ym:s:deviceCategory", d1, d2, 5)
    if devs:
        last = api.goals()[-1][0] if api.goals() else ""
        parts = []
        for name, v in devs.items():
            s = f"{name}: {int(v['visits'])} виз., отказы {v['bounce']:.0f}%"
            if last and v["visits"]:
                s += f", {last} {v.get(last, 0) / v['visits'] * 100:.1f}%"
            parts.append(s)
        lines.append("По устройствам: " + " · ".join(parts))
    return "\n".join(lines)


async def weekly() -> str:
    if time.time() - (store.get("collected") or {}).get("ts", 0) > 6 * 3600:
        await collect(3)
    w1, w2 = _period(7)
    p1, p2 = _period(7, dt.date.fromisoformat(w1) - dt.timedelta(days=1))
    cur, prev = _one(store.totals(w1, w2)), _one(store.totals(p1, p2))
    lines = [f"📈 Директ — неделя {dt.date.fromisoformat(w1):%d.%m}–{dt.date.fromisoformat(w2):%d.%m}",
             f"Расход {rub(cur['cost'])}{pct(cur['cost'], prev['cost'])} · кликов {cur['clicks']}{pct(cur['clicks'], prev['clicks'])}"
             f" · CPC {rub(cur['cpc'])}",
             f"Качественных визитов {cur['quality']}{pct(cur['quality'], prev['quality'])} · за визит {rub(cur['cpq'])}"
             f"{pct(cur['cpq'] or 0, prev['cpq'] or 0) if cur['cpq'] and prev['cpq'] else ''}",
             "(в скобках — к прошлой неделе)", "", "Кампании:"]
    for c in sorted(store.totals(w1, w2, "campaign_id"), key=lambda r: -(r.get("cost") or 0)):
        if c.get("clicks"):
            lines.append("∙ " + _line(f"«{store.campaign_name(c['campaign_id'])}»", c))
    devs = [d for d in store.totals(w1, w2, "device") if d.get("clicks")]
    if len(devs) > 1:
        lines.append("\nУстройства:")
        lines += ["∙ " + _line(DEV.get(d["device"], d["device"]), d) for d in devs]
    met = {}
    try:
        met = _metrika_by_article(await api.metrika_by("ym:s:startURL", dt.date.fromisoformat(w1),
                                                       dt.date.fromisoformat(w2), 1000))
    except Exception as e:
        log.warning("metrika by article: %s", e)
    arts = _ranked_articles(w1, w2)
    if arts:
        lines.append(f"\nЛучшие статьи (от {ART_MIN_CLICKS} кликов, цена качественного визита):")
        lines += [f"{i}. {_art_line(r, met.get(r['article']))}" for i, r in enumerate(arts[:5], 1)]
        if len(arts) > 5:
            lines.append("\nХудшие:")
            lines += [f"∙ {_art_line(r, met.get(r['article']))}" for r in arts[-5:][::-1] if r not in arts[:5]]
    young = [r for r in store.by_article(w1, w2) if 0 < (r.get("clicks") or 0) < ART_MIN_CLICKS]
    if young:
        lines.append(f"\nРано судить (меньше {ART_MIN_CLICKS} кликов): {len(young)} статей.")
    good = sorted([quality(r) for r in store.by_placement(w1, w2) if (r.get("clicks") or 0) >= JUNK_MIN_CLICKS
                   and not OWN.search(r["placement"] or "")], key=lambda r: r["cpq"] if r["cpq"] else 1e9)
    bad = {r["placement"] for r in junk(store.by_placement(w1, w2))}
    good = [r for r in good if r["placement"] not in bad and store.place_status(r["placement"]) != "excluded"][:5]
    if good:
        lines.append("\nЛучшие площадки РСЯ:")
        lines += [f"∙ {r['placement']} — {rub(r['cpq'])} за визит, отказы {r['bounce_rate'] or 0:.0f}%, {r['clicks']} кл."
                  for r in good]
    excl = store.places("excluded")
    week_ago = time.time() - 7 * 86400
    lines.append(f"\nИсключено площадок всего: {len(excl)}, за неделю: {sum(1 for p in excl if p['ts'] > week_ago)}.")
    await say("\n".join(lines))
    try:
        await say(await funnel_text(dt.date.fromisoformat(w1), dt.date.fromisoformat(w2)))
    except Exception as e:
        await say(f"Воронка не посчиталась: {e}")
    rows, batch = propose_places()
    if rows:
        await say(places_text(rows, 10), place_buttons(rows, batch))
    return f"неделя {rub(cur['cost'])}, статей с данными {len(arts)}"


# ---------- исключение площадок (только по кнопке владельца) ----------

async def exclude(placements: list[str], reason: str = "мусорная площадка, по кнопке владельца") -> str:
    since = _iso(_today() - dt.timedelta(days=60))
    per_camp: dict[int, list[str]] = {}
    for p in placements:
        for cid in store.campaigns_with_place(p, since):
            per_camp.setdefault(cid, []).append(p)
    if not per_camp:
        return "Не нашёл, в каких кампаниях были эти площадки."
    out, done = [], set()
    for cid, sites in per_camp.items():
        try:
            added, note = await api.add_excluded(cid, sites)
            store.log_action("exclude_sites", cid, added, reason, True, note)
            done.update(s.lower() for s in sites)
            out.append(f"✅ «{store.campaign_name(cid)}»: +{len(added)}" + (f" ({note})" if note else ""))
        except Exception as e:
            store.log_action("exclude_sites", cid, sites, reason, False, str(e)[:300])
            out.append(f"⚠️ «{store.campaign_name(cid)}»: не получилось — {e}")
    for p in placements:
        if p.lower() in done:
            store.set_place(p, "excluded", reason)
    return "\n".join(out)


def keep(placements: list[str]):
    for p in placements:
        store.set_place(p, "keep", "владелец: не трогать")


def proposal_items(batch: int | None = None) -> list[str]:
    p = store.get("proposal") or {}
    if batch is not None and p.get("id") != batch:
        return []
    return [x for x in p.get("items", []) if store.place_status(x) == "proposed"]


# ---------- статус и расписание ----------

def status() -> str:
    if not api.enabled():
        return ("📈 Директ-агент: нет токена.\nНужны DIRECT_TOKEN (доступ к API Директа) и, если аккаунт агентский, "
                "DIRECT_CLIENT_LOGIN. Пока заявка на API не одобрена, можно DIRECT_SANDBOX=1.")
    camps = store.campaigns()
    on = sum(1 for c in camps if c["state"] == "ON")
    col = store.get("collected") or {}
    lines = [f"📈 Директ-агент · {'ПЕСОЧНИЦА' if api.sandbox() else 'боевой аккаунт'}"
             + (f" · клиент {config.env('DIRECT_CLIENT_LOGIN')}" if config.env("DIRECT_CLIENT_LOGIN") else ""),
             f"Кампаний: {len(camps)}, включено {on}",
             f"Данные: {col.get('from', '—')} — {col.get('to', '—')}" if col else "Данные ещё не собирались",
             f"Метрика: {'есть' if api.has_metrika() else 'нет доступа'} · цели воронки: "
             + (", ".join(n for n, _ in api.goals()) or "не заданы (DIRECT_GOALS)"),
             f"Потолок расхода в день: {rub(DAILY_CAP) if DAILY_CAP else 'не задан (DIRECT_DAILY_CAP)'}",
             f"Площадок исключено: {len(store.places('excluded'))}, на рассмотрении: {len(store.places('proposed'))}, "
             f"не трогать: {len(store.places('keep'))}"]
    if api.units():
        lines.append(f"Баллы API (потрачено/осталось/лимит): {api.units()}")
    lines.append("\nПоследние запуски:")
    for r in store.runs():
        t = dt.datetime.fromtimestamp(r["ts"], ZoneInfo(config.SEO_TZ))
        lines.append(f"{'✅' if r['ok'] else '⚠️'} {r['name']} — {t:%d.%m %H:%M}: {r['note']}")
    acts = store.actions(5)
    if acts:
        lines.append("\nПоследние действия в Директе:")
        for a in acts:
            t = dt.datetime.fromtimestamp(a["ts"], ZoneInfo(config.SEO_TZ))
            lines.append(f"{'✅' if a['ok'] else '⚠️'} {t:%d.%m %H:%M} {a['kind']} · «{store.campaign_name(a['campaign_id'])}»"
                         + (f" · {a['note']}" if a["note"] else ""))
    return "\n".join(lines)


def due(name: str) -> bool:
    now = _now()
    last = store.last_run(name)
    tried = store.get(f"try:{name}", 0)
    if name == "hourly":
        return time.time() - tried > 55 * 60
    if time.time() - tried < 30 * 60:          # после неудачи — не чаще раза в 30 минут
        return False
    done_today = last and dt.datetime.fromtimestamp(last, ZoneInfo(config.SEO_TZ)).date() == now.date()
    if name == "daily":
        return (now.hour >= REPORT_HOUR and not done_today) or not store.has_stats()
    if name == "weekly":
        return now.weekday() == 0 and (now.hour, now.minute) >= (10, 15) and not done_today
    return False


JOBS = {"hourly": hourly, "daily": daily, "weekly": weekly}


async def run(name: str) -> str:
    if name in _running:
        return "уже выполняется"
    _running.add(name)
    store.put(f"try:{name}", time.time())
    try:
        note = await JOBS[name]()
        store.mark_run(name, True, note)
        store.put(f"err:{name}", False)
        log.info("direct %s: %s", name, note)
        return note
    except Exception as e:
        log.exception("direct %s failed", name)
        store.mark_run(name, False, f"ошибка: {e}"[:300])
        if not store.get(f"err:{name}"):          # об одной и той же поломке пишем один раз, до первого успеха
            await say(f"⚠️ Директ ({name}) упал: {e}")
        store.put(f"err:{name}", True)
        return f"ошибка: {e}"
    finally:
        _running.discard(name)


async def loop():
    if not ENABLED:
        log.info("Директ-агент выключен (DIRECT_ENABLED=0)")
        return
    store.db()
    await asyncio.sleep(70)
    while True:
        if api.enabled() and not _running:
            for name in ("daily", "weekly", "hourly"):     # по одной задаче за раз
                try:
                    if due(name):
                        asyncio.create_task(run(name))
                        break
                except Exception:
                    log.exception("direct scheduler")
        await asyncio.sleep(60)
