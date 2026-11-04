"""Расписание SEO-мониторинга внутри процесса бота (время — по SEO_TZ, по умолчанию Москва).

hourly     — каждый час
new_pages  — каждые 30 минут
daily      — каждый день после 08:30
weekly     — по понедельникам после 09:00

Время последних запусков хранится в data/seo.db, поэтому перезапуск бота (автообновление) расписание не сбивает.
Задачи одного вида не накладываются; недельный обход идёт в фоне и не мешает часовым проверкам.
"""
import asyncio
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

from core import config
from core.seo import monitor, store

log = logging.getLogger("seo.scheduler")
_running: set[str] = set()


def _now() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(config.SEO_TZ))


def _local(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, ZoneInfo(config.SEO_TZ))


def due(name: str, now: dt.datetime | None = None) -> bool:
    now = now or _now()
    last = store.last_run(name)
    if name == "hourly":
        return time.time() - last >= 3600
    if name == "new_pages":
        return time.time() - last >= 1800
    if name == "daily":
        return (now.hour, now.minute) >= (8, 30) and (not last or _local(last).date() < now.date())
    if name.startswith("hubs_") and not config.HUBS_ENABLED:
        return False
    if name == "hubs_quick":
        return time.time() - last >= 1800
    if name == "hubs_daily":
        return now.hour >= 7 and (not last or _local(last).date() < now.date())
    if name == "hubs_weekly":
        return now.weekday() == 0 and now.hour >= 10 and (not last or _local(last).date() < now.date())
    if name == "hubs_monthly":
        return now.day == 1 and now.hour >= 11 and (not last or _local(last).strftime("%Y-%m") < now.strftime("%Y-%m"))
    if name == "bulk":
        from core.seo import bulk
        return bulk.state().get("phase") == "running" and time.time() - last >= 3600
    if name in ("content_plan", "monthly") and not monitor.autopilot():
        return False                 # автопилот выключен — ИИ-задачи только по командам
    if name == "content_plan":   # вторник 10:00 — после недельного отчёта
        return now.weekday() == 1 and now.hour >= 10 and (not last or _local(last).date() < now.date())
    if name == "monthly":
        return now.day == 1 and now.hour >= 10 and (not last or _local(last).strftime("%Y-%m") < now.strftime("%Y-%m"))
    if name == "weekly":
        if now.weekday() != 0 or now.hour < 9:
            return False
        return not last or _local(last).date() < now.date()
    return False


JOBS = {
    "hourly": monitor.hourly,
    "new_pages": monitor.new_pages,
    "daily": monitor.daily,
    "weekly": monitor.weekly,
    "monthly": monitor.monthly,
    "content_plan": monitor.content_plan,
    "bulk": lambda: __import__("core.seo.bulk", fromlist=["tick"]).tick(monitor.say),
    "hubs_quick": lambda: __import__("core.seo.hubs", fromlist=["quick"]).quick(),
    "hubs_daily": lambda: __import__("core.seo.hubs", fromlist=["daily"]).daily(),
    "hubs_weekly": lambda: __import__("core.seo.hubs", fromlist=["weekly"]).weekly(monitor.say),
    "hubs_monthly": lambda: __import__("core.seo.hubs", fromlist=["monthly"]).monthly(monitor.say),
}


async def run(name: str) -> str:
    """Запустить проверку сейчас (из расписания или по команде /seo_scan)."""
    if name in _running:
        return "уже выполняется"
    _running.add(name)
    from core.office import office
    try:
        res = await JOBS[name]()
        note = res[0] if isinstance(res, tuple) else res
        store.mark_run(name, True, note or "")
        log.info("seo %s: %s", name, note)
        return note or "готово"
    except Exception as e:
        log.exception("seo %s failed", name)
        store.mark_run(name, False, f"ошибка: {e}")
        office.emit("seo", "error", f"Мониторинг ({name}) упал: {e}")
        return f"ошибка: {e}"
    finally:
        _running.discard(name)


async def loop():
    if not config.SEO_MONITOR:
        log.info("SEO-мониторинг выключен (SEO_MONITOR=0)")
        return
    await asyncio.sleep(20)  # дать боту подняться
    # robots.txt нужен new_pages, чтобы найти sitemap — поэтому часовая проверка первой
    order = ["hourly", "new_pages", "daily", "weekly", "monthly", "content_plan", "bulk",
             "hubs_quick", "hubs_daily", "hubs_weekly", "hubs_monthly"]
    while True:
        for name in order:
            try:
                if name not in _running and due(name):
                    if name in ("weekly", "monthly", "bulk", "hubs_daily", "hubs_weekly", "hubs_monthly"):
                        asyncio.create_task(run(name))   # долгие — в фоне
                    else:
                        await run(name)
            except Exception:
                log.exception("scheduler")
        await asyncio.sleep(60)


# ---------- расписание для ИИ-офиса (вкладка «Расписание») ----------

def schedule_info() -> dict:
    """Что, когда и кто делает сам — с последним запуском и тем, включено ли."""
    from core import config, site_api
    from core.seo import author_tips, bulk
    runs = {r["name"]: r for r in store.runs()}
    auto = monitor.autopilot()

    def last(name):
        r = runs.get(name)
        if not r:
            return None
        return {"ts": r["ts"], "ok": bool(r["ok"]), "note": r["note"]}

    seo = [
        {"when": "каждый час", "what": "Сайт отвечает, robots.txt не закрыл сайт, SSL не истекает",
         "out": "алерт в Telegram, только если сломалось или починилось", "ai": False, "on": config.SEO_MONITOR,
         "last": last("hourly")},
        {"when": "каждые 30 минут", "what": "Новые адреса в sitemap: проверка страницы, переобход в Яндексе, IndexNow",
         "out": "алерт только при критичной ошибке на новой странице", "ai": False, "on": config.SEO_MONITOR,
         "last": last("new_pages")},
        {"when": "каждые 30 минут", "what": "Новая статья автора → проверка и письмо автору с советами (~1–2 ¢)",
         "out": "письмо тебе на «Ок»" + (" → уйдёт автору сам" if config.SEO_TIPS_AUTO_SEND and site_api.enabled() else ""),
         "ai": True, "on": author_tips.enabled(), "switch": "/seo_tips on|off", "last": last("new_pages")},
        {"when": "каждые 30 минут", "what": "Новая статья редакции → пакет SEO-оформления (до "
         f"{config.SEO_AUTO_OPTIMIZE_PER_DAY} в день)", "out": "пакет тебе на «Ок»", "ai": True, "on": auto,
         "switch": "/seo_auto on|off", "last": None},
        {"when": "ежедневно 08:30", "what": "Вебмастер (страниц в поиске, ошибки) и Метрика (трафик к прошлой неделе)",
         "out": "алерт только при просадке", "ai": False, "on": config.SEO_MONITOR, "last": last("daily")},
        {"when": "понедельник 09:00", "what": "Полный обход сайта, недельный отчёт",
         "out": "файл отчёта" + (" + список задач от агента" if auto else ""), "ai": False, "on": config.SEO_MONITOR,
         "last": last("weekly")},
        {"when": "вторник 10:00", "what": "Контент-план на неделю: что писать и что обновлять", "out": "план тебе на «Ок»",
         "ai": True, "on": auto, "switch": "/seo_auto on|off", "last": last("content_plan")},
        {"when": "1-е число 10:00", "what": "Разбор результатов за месяц и новые правила для агента",
         "out": "отчёт + правила на «Ок»", "ai": True, "on": auto, "switch": "/seo_auto on|off", "last": last("monthly")},
        {"when": "каждые 30 минут", "what": "Хабы /education: новые посты в ленты «Свежее», удалённые статьи — из блоков",
         "out": "hubs.json сам, без «Ок»", "ai": True, "on": config.HUBS_ENABLED, "last": last("hubs_quick")},
        {"when": "ежедневно 07:00", "what": "Хабы на сайте отвечают и показывают свежую версию, статьи из блоков живы",
         "out": "алерт только при проблеме", "ai": False, "on": config.HUBS_ENABLED, "last": last("hubs_daily")},
        {"when": "понедельник 10:00", "what": "Хабы: новые подборки во всех блоках + трафик хабов за неделю",
         "out": "черновик тебе на «Ок»" if not config.HUBS_AUTO_APPLY else "публикуется сам, тебе отчёт", "ai": True,
         "on": config.HUBS_ENABLED, "switch": "/hubs_build", "last": last("hubs_weekly")},
        {"when": "1-е число 11:00", "what": "Хабы: пересмотр структуры — новые блоки и подстраницы по спросу",
         "out": "предложения на «Ок»", "ai": False, "on": config.HUBS_ENABLED, "last": last("hubs_monthly")},
        {"when": "каждый час, пока идёт", "what": "Массовое оформление старых статей (/seo_bulk)",
         "out": "раз в день отчёт о ходе", "ai": True, "on": bulk.state().get("phase") == "running",
         "switch": "/seo_bulk", "last": last("bulk")},
    ]
    only_tasks = "Сам ничего не запускает — только задачи от тебя (бот или офис) и просьбы коллег внутри твоих задач."
    return {
        "tz": config.SEO_TZ,
        "agents": [
            {"id": "seo", "title": "SEO-шник", "items": seo,
             "note": "Без ИИ — бесплатно. С ИИ — тратит API, включается переключателем."},
            {"id": "designer", "title": "Графический дизайнер", "items": [], "note": only_tasks},
            {"id": "webdesigner", "title": "Веб-дизайнер", "items": [], "note": only_tasks},
            {"id": "smm", "title": "SMM-щик", "items": [], "note": only_tasks + " Публикует в каналы только после «Ок»."},
        ],
    }
