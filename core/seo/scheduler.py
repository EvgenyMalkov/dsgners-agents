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
    order = ["hourly", "new_pages", "daily", "weekly"]
    while True:
        for name in order:
            try:
                if name not in _running and due(name):
                    if name == "weekly":
                        asyncio.create_task(run(name))   # долгий — в фоне
                    else:
                        await run(name)
            except Exception:
                log.exception("scheduler")
        await asyncio.sleep(60)
