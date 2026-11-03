"""Запуск задач агентов — общий для офиса и Telegram-бота."""
import asyncio
import logging
import shutil
import time

from core import config, feedback
from core.agent import run_agent
from core.office import AGENTS, office

_locks = {a: asyncio.Lock() for a in AGENTS}
_listeners: list = []   # async callable(job) — задача готова к согласованию
_approvers: list = []   # async callable(job) — задача принята (например, SMM публикует посты)


def on_review(cb):
    _listeners.append(cb)


def on_approved(cb):
    _approvers.append(cb)


def _short(s: str, n=70) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _recent_context(agent: str, n: int = 3) -> str:
    """Последние задачи агента — чтобы короткие реплики вроде «а в брендбуке лучше вариант…» понимались как правка."""
    import json
    import time as _t
    recent = [j for j in office.list_jobs(agent, 10)
              if j["status"] in ("review", "approved", "redo") and _t.time() - (j["created"] or 0) < 3 * 86400][:n]
    if not recent:
        return ""
    lines = []
    for j in recent:
        renders = []
        for f in j["files"][:4]:
            pj = (config.OUTBOX / f).with_suffix(".json")
            if pj.exists():
                try:
                    d = json.loads(pj.read_text("utf-8"))
                    renders.append(f"{f.split('/')[-1]} ← {d.get('template')}: " + json.dumps(d.get("data", {}), ensure_ascii=False)[:500])
                except Exception:
                    pass
        lines.append(f"- [{j['status']}] «{_short(j['task'], 120)}» → {_short(j.get('caption') or '', 160)}"
                     + ("".join(f"\n    {r}" for r in renders) if renders else ""))
    return ("\n\n# Твои недавние задачи (для контекста)\n" + "\n".join(lines) +
            "\nЕсли новая задача похожа на правку или комментарий к одной из них («поменяй», «лучше вариант», "
            "«не тот шаблон»…) — переделай ту работу с учётом комментария, взяв её параметры за основу. "
            "Если сообщение явно оборвано или непонятно — сделай лучший вариант по смыслу и в подписи скажи, как понял.")


def start_job(agent: str, task: str, source: str, parent: str | None = None, full_task: str | None = None) -> str:
    if agent not in AGENTS or not AGENTS[agent]["active"]:
        raise ValueError(f"Агент {agent} пока не работает")
    if parent is None and not source.startswith("agent:"):
        full_task = (full_task or task) + _recent_context(agent)
    jid = office.create_job(agent, task, full_task or task, source, parent)
    if _locks[agent].locked():
        office.emit(agent, "info", f"В очереди: {_short(task)}", jid)
    asyncio.create_task(_run(jid, agent))
    return jid


async def _execute(jid: str, agent: str, internal_for: str | None = None) -> list:
    """Выполнить задачу под замком агента. Возвращает список файлов."""
    async with _locks[agent]:
        job = office.get_job(jid)
        task = office.full_task(jid)
        office.update_job(jid, status="running")
        office.set_status(agent, "thinking", _short(job["task"], 40))
        who = f" (просит {AGENTS[internal_for]['title']})" if internal_for else ""
        office.emit(agent, "info", f"Взял задачу{who}: {_short(job['task'])}", jid)

        async def progress(txt: str):
            office.set_status(agent, "working", _short(txt, 40))
            office.emit(agent, "tool", txt, jid)

        try:
            res = await run_agent(agent, task, progress=progress, job_id=jid)
        except Exception as e:
            logging.exception("agent failed")
            office.update_job(jid, status="error", caption=str(e), finished=time.time())
            office.emit(agent, "error", f"Ошибка: {e}", jid)
            office.set_status(agent, "idle")
            return []

        files = [str(p.relative_to(config.OUTBOX)) for p in res["files"]]
        if internal_for:
            status = "sub" if files else "empty"
        else:
            status = "review" if files else "empty"
        office.update_job(jid, status=status, files=files, caption=res["text"], cost=res["cost"],
                          meta=res.get("meta") or {}, finished=time.time())
        if not files:
            office.emit(agent, "error", "Ничего не сдал: " + _short(res["text"], 120), jid)
        elif internal_for:
            office.emit(agent, "done", f"Передал коллеге ({AGENTS[internal_for]['title']}): {_short(job['task'])}", jid)
        else:
            office.emit(agent, "approval", f"Жду подтверждения: {_short(job['task'])}", jid)
        office.set_status(agent, "idle")
        return files


async def run_internal(agent: str, task: str, parent: str | None, requester: str) -> dict:
    """Задача от одного агента другому: выполняется сразу, без согласования, результат возвращается агенту."""
    if agent not in AGENTS or not AGENTS[agent]["active"]:
        return {"status": "error", "caption": f"Коллега {agent} сейчас не работает", "files": []}
    jid = office.create_job(agent, task, task, f"agent:{requester}", parent)
    await _execute(jid, agent, internal_for=requester)
    return office.get_job(jid)


async def _run(jid: str, agent: str):
    files = await _execute(jid, agent)
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
    job = office.get_job(jid)
    for cb in _approvers:
        asyncio.create_task(_safe(cb, job))
    return job


async def _safe(cb, job):
    try:
        await cb(job)
    except Exception as e:
        logging.exception("approve hook failed")
        office.emit(job["agent"], "error", f"После принятия: {e}", job["id"])


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
