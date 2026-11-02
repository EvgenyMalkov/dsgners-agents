"""Правки владельца → правила агента. Живой файл data/feedback/<агент>.md (не в git).
Хорошие правила раз в неделю переносим в agents/<агент>/feedback.md и коммитим."""
import datetime as dt

from core import config


def path(agent: str):
    return config.DATA / "feedback" / f"{agent}.md"


def add_rule(agent: str, reason: str, task: str = "") -> None:
    p = path(agent)
    note = f"  _(задача: {task.strip()[:80]})_" if task else ""
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- {dt.date.today():%d.%m.%Y}: {reason.strip()}{note}\n")


def read(agent: str) -> str:
    p = path(agent)
    return p.read_text("utf-8") if p.exists() else ""
