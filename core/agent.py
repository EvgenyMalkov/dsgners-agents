"""Общий движок агентов.

Агент = папка agents/<имя>/ с файлами:
  ROLE.md       — роль и порядок работы
  checklist.md  — чек-лист качества (необязательно)
  feedback.md   — утверждённые правила (в git)
Тексты можно переопределить из офиса (data/agents/<имя>/), документы и референсы — core/knowledge.py.
  tools.py      — TOOLS (описания для модели) и async handle(name, input, ctx)
Плюс общий мозг team/*.md и живые правила data/feedback/<имя>.md (пополняются из Telegram).
"""
import base64
import datetime as dt
import importlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from PIL import Image

from core import config, knowledge, spend


@dataclass
class Ctx:
    agent: str
    job_dir: Path
    files: list[Path] = field(default_factory=list)
    caption: str = ""
    done: bool = False
    progress: object = None  # async callable(str) — статус для бота/офиса


def _read(p: Path) -> str:
    return p.read_text("utf-8") if p.exists() else ""


def build_system(agent: str, extra: str = "") -> str:
    kn = knowledge
    docs = kn.docs_text(agent)
    parts = [
        "# Общий мозг команды dsgners\n" + _read(config.TEAM / "brand.md"),
        _read(config.TEAM / "rules.md"),
        f"# Твоя роль: {agent}\n" + kn.load_text(agent, "ROLE.md"),
        kn.load_text(agent, "checklist.md"),
        "# Правила из прошлых правок владельца (соблюдай строго)\n"
        + kn.load_text(agent, "feedback.md") + "\n" + kn.load_text(agent, "live-rules.md"),
        ("# Справочные документы\n" + docs) if docs else "",
        extra,
    ]
    return "\n\n".join(p.strip() for p in parts if p.strip())


def first_message(agent: str, task: str) -> list:
    """Задача + референсы «хорошо/плохо» (картинки), если они загружены в офисе."""
    content: list = []
    for kind, head, limit in (("good", "Референсы «так хорошо» — ориентируйся на них:", 4),
                              ("bad", "Антиреференсы «так плохо» — так не делай:", 3)):
        imgs = knowledge.refs(agent, kind)[:limit]
        if not imgs:
            continue
        content.append({"type": "text", "text": head})
        for img in imgs:
            content.append(image_block(img, 700))
            note = knowledge.note_for(img)
            if note:
                content.append({"type": "text", "text": f"↑ {note}"})
    if content:
        content[-1]["cache_control"] = {"type": "ephemeral"}
    content.append({"type": "text", "text": "# Задача\n" + task})
    return content


def image_block(png: Path, max_side: int = 1100) -> dict:
    """PNG → блок изображения для модели (уменьшаем, чтобы экономить токены)."""
    im = Image.open(png).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


def _prune_old_images(messages: list) -> None:
    """Старые картинки в истории заменяем текстом — модель видит только свежий рендер."""
    for msg in messages[:-1]:
        if msg["role"] != "user" or not isinstance(msg["content"], list):
            continue
        for part in msg["content"]:
            if part.get("type") == "tool_result" and isinstance(part.get("content"), list):
                part["content"] = [c if c.get("type") != "image" else {"type": "text", "text": "[старый рендер скрыт]"}
                                   for c in part["content"]]


async def run_agent(agent: str, task: str, progress=None) -> dict:
    if spend.this_month() >= config.MONTHLY_LIMIT_USD:
        raise RuntimeError(f"Достигнут месячный лимит ${config.MONTHLY_LIMIT_USD:.0f} — подними MONTHLY_LIMIT_USD в .env")

    tools_mod = importlib.import_module(f"agents.{agent}.tools")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    ctx = Ctx(agent=agent, job_dir=config.OUTBOX / f"{stamp}-{agent}", progress=progress)
    ctx.job_dir.mkdir(parents=True, exist_ok=True)

    extra = getattr(tools_mod, "system_extra", lambda: "")()
    system = [{"type": "text", "text": build_system(agent, extra), "cache_control": {"type": "ephemeral"}}]
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    messages: list = [{"role": "user", "content": first_message(agent, task)}]
    cost = 0.0
    final_text = ""

    for _ in range(config.MAX_STEPS):
        _prune_old_images(messages)
        resp = await client.messages.create(
            model=config.MODEL, max_tokens=4096, system=system, tools=tools_mod.TOOLS, messages=messages,
        )
        cost += spend.record(agent, resp.usage)
        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        final_text = "\n".join(b.text for b in resp.content if b.type == "text").strip() or final_text

        if resp.stop_reason != "tool_use":
            break
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            try:
                out = await tools_mod.handle(b.name, b.input, ctx)
            except Exception as e:  # ошибку инструмента отдаём модели, пусть исправится
                out = [{"type": "text", "text": f"Ошибка инструмента: {e}"}]
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        messages.append({"role": "user", "content": results})
        if ctx.done:
            break

    (ctx.job_dir / "task.md").write_text(task, "utf-8")
    return {"files": ctx.files, "text": ctx.caption or final_text, "cost": cost, "dir": ctx.job_dir}
