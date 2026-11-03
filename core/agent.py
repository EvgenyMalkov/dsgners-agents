"""Общий движок агентов.

Агент = папка agents/<имя>/ с файлами:
  ROLE.md       — роль и порядок работы
  checklist.md  — чек-лист качества (необязательно)
  feedback.md   — утверждённые правила (в git)
Тексты можно переопределить из офиса (data/agents/<имя>/), документы и референсы — core/knowledge.py.
  tools.py      — TOOLS (описания для модели) и async handle(name, input, ctx)
  skills/       — навыки (SKILL.md), см. core/skills.py
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

from core import config, knowledge, skills, spend


@dataclass
class Ctx:
    agent: str
    job_dir: Path
    files: list[Path] = field(default_factory=list)
    caption: str = ""
    done: bool = False
    progress: object = None  # async callable(str) — статус для бота/офиса
    job_id: str | None = None
    meta: dict = field(default_factory=dict)  # доп. данные результата (посты SMM и т.п.)


def _read(p: Path) -> str:
    return p.read_text("utf-8") if p.exists() else ""


def build_system(agent: str, extra: str = "") -> str:
    kn = knowledge
    docs = kn.docs_text(agent)
    parts = [
        "# Общий мозг команды dsgners\n" + _read(config.TEAM / "brand.md"),
        _read(config.TEAM / "rules.md"),
        _read(config.TEAM / "brandbook.md"),
        f"# Твоя роль: {agent}\n" + kn.load_text(agent, "ROLE.md"),
        kn.load_text(agent, "checklist.md"),
        "# Правила из прошлых правок владельца (соблюдай строго)\n"
        + kn.load_text(agent, "feedback.md") + "\n" + kn.load_text(agent, "live-rules.md"),
        ("# Справочные документы\n" + docs) if docs else "",
        skills.system_section(agent),
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


def _cache_tail(messages: list) -> None:
    """Скользящая точка кеша на последнем сообщении: вся история шагов читается из кеша (в 10 раз дешевле),
    а не оплачивается заново на каждом шаге. Точек кеша не больше 4: системный промпт, первое сообщение
    (референсы/задача), и эта — на конце истории."""
    for msg in messages[1:]:
        if isinstance(msg["content"], list):
            for part in msg["content"]:
                if isinstance(part, dict):
                    part.pop("cache_control", None)
    last = messages[-1]["content"]
    if isinstance(last, list) and last and isinstance(last[-1], dict):
        last[-1]["cache_control"] = {"type": "ephemeral"}


# История шагов НЕ меняется задним числом: модель «думает» (thinking), и API проверяет, что всё, что было
# до её размышлений, осталось байт-в-байт тем же. Раньше старые картинки заменялись текстом — это ломало
# запросы («Invalid signature in thinking block»). Расход держим кешем истории (_cache_tail): старые
# картинки читаются из кеша по 0.1 цены.


async def _use_skill(inp: dict, ctx: Ctx):
    slug = (inp.get("skill") or "").strip().strip("`")
    text = skills.skill_text(ctx.agent, slug)
    if ctx.progress:
        await ctx.progress(f"взял навык «{slug}»")
    return [{"type": "text", "text": f"# Навык {slug}\n{text}"}]


async def run_agent(agent: str, task: str, progress=None, job_id: str | None = None) -> dict:
    if spend.this_month() >= config.MONTHLY_LIMIT_USD:
        raise RuntimeError(f"Достигнут месячный лимит ${config.MONTHLY_LIMIT_USD:.0f} — подними MONTHLY_LIMIT_USD в .env")

    tools_mod = importlib.import_module(f"agents.{agent}.tools")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    ctx = Ctx(agent=agent, job_dir=config.OUTBOX / f"{stamp}-{agent}-{(job_id or 'x')[:6]}", progress=progress, job_id=job_id)
    ctx.job_dir.mkdir(parents=True, exist_ok=True)

    extra = getattr(tools_mod, "system_extra", lambda: "")()
    system = [{"type": "text", "text": build_system(agent, extra), "cache_control": {"type": "ephemeral"}}]
    tools = list(tools_mod.TOOLS)
    if skills.list_skills(agent, enabled_only=True):
        tools.append(skills.USE_SKILL_TOOL)
    model = (getattr(tools_mod, "model_for", lambda t: None)(task) or config.MODEL)
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    messages: list = [{"role": "user", "content": first_message(agent, task)}]
    cost = 0.0
    final_text = ""

    # агент может поднять лимиты у себя в tools.py (длинные статьи требуют больше шагов и токенов)
    for _ in range(getattr(tools_mod, "MAX_STEPS", config.MAX_STEPS)):
        _cache_tail(messages)
        resp = await client.messages.create(
            model=model, max_tokens=getattr(tools_mod, "MAX_TOKENS", 8192), system=system, tools=tools, messages=messages,
        )
        cost += spend.record(agent, resp.usage, model)
        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        final_text = "\n".join(b.text for b in resp.content if b.type == "text").strip() or final_text

        if resp.stop_reason == "pause_turn":
            continue
        if resp.stop_reason != "tool_use":
            break
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            try:
                if b.name == "use_skill":
                    out = await _use_skill(b.input, ctx)
                else:
                    out = await tools_mod.handle(b.name, b.input, ctx)
            except Exception as e:  # ошибку инструмента отдаём модели, пусть исправится
                out = [{"type": "text", "text": f"Ошибка инструмента: {e}"}]
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        messages.append({"role": "user", "content": results})
        if ctx.done:
            break

    (ctx.job_dir / "task.md").write_text(task, "utf-8")
    return {"files": ctx.files, "text": ctx.caption or final_text, "cost": cost, "dir": ctx.job_dir, "meta": ctx.meta}
