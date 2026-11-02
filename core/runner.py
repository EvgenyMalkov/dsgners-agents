"""Запуск задач агентов — общий для офиса и Telegram-бота."""
import asyncio
import logging
import shutil
import time

from core import config, feedback
from core.agent import run_agent
from core.office import AGENTS, office

_locks = {a: asyncio.Lock() for a in AGENTS}
_listeners: list = []  # async callable(job) — вызывается, когда задача готова к согласованию


def on_review(cb):
    _listeners.append(cb)


def _short(s: str, n=70) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def start_job(agent: str, task: str, source: str, parent: str | None = None, full_task: str | None = None) -> str:
    if agent not in AGENTS or not AGENTS[agent]["active"]:
        raise ValueError(f"Агент {agent} пока не работает")
    jid = office.create_job(agent, task, full_task or task, source, parent)
    if _locks[agent].locked():
        office.emit(agent, "info", f"В очереди: {_short(task)}", jid)
    asyncio.create_task(_run(jid, agent))
    return jid


async def _run(jid: str, agent: str):
    async with _locks[agent]:
        job = office.get_job(jid)
        task = office.full_task(jid)
        office.update_job(jid, status="running")
        office.set_status(agent, "thinking", _short(job["task"], 40))
        office.emit(agent, "info", f"Взял задачу: {_short(job['task'])}", jid)

        async def progress(txt: str):
            office.set_status(agent, "working", _short(txt, 40))
            office.emit(agent, "tool", txt, jid)

        try:
            res = await run_agent(agent, task, progress=progress)
        except Exception as e:
            logging.exception("agent failed")
            office.update_job(jid, status="error", caption=str(e), finished=time.time())
            office.emit(agent, "error", f"Ошибка: {e}", jid)
            office.set_status(agent, "idle")
            return

        files = [str(p.relative_to(config.OUTBOX)) for p in res["files"]]
        status = "review" if files else "empty"
        office.update_job(jid, status=status, files=files, caption=res["text"], cost=res["cost"], finished=time.time())
        if files:
            office.emit(agent, "approval", f"Жду подтверждения: {_short(job['task'])}", jid)
        else:
            office.emit(agent, "error", "Ничего не сдал: " + _short(res["text"], 120), jid)
        office.set_status(agent, "idle")

    if files:
        for cb in _listeners:
            try:
                await cb(office.get_job(jid))
            except Exception:
                logging.exception("listener failed")


def approve(jid: str) -> dict:
    job = office.get_job(jid)
    if not job:
        raise ValueError("Нет такой задачи")
    if job["status"] != "review":
        return job
    approved = config.OUTBOX / "approved"
    approved.mkdir(exist_ok=True)
    for f in job["files"]:
        src = config.OUTBOX / f
        if src.exists():
            shutil.copy(src, approved / f.replace("/", "-"))
    office.update_job(jid, status="approved")
    office.emit(job["agent"], "done", f"Принято: {_short(job['task'])}", jid)
    return office.get_job(jid)


def redo(jid: str, reason: str, source: str) -> str:
    job = office.get_job(jid)
    if not job:
        raise ValueError("Нет такой задачи")
    if job["status"] not in ("review", "approved", "empty", "error"):
        raise ValueError("Эту задачу сейчас нельзя переделать")
    feedback.add_rule(job["agent"], reason, job["task"])
    office.update_job(jid, status="redo", reason=reason)
    office.emit(job["agent"], "message", f"Правка: {_short(reason, 120)}", jid)
    full = f"{office.full_task(jid)}\n\nПредыдущий вариант отклонён владельцем. Причина: {reason}"
    return start_job(job["agent"], job["task"], source, parent=jid, full_task=full)
