"""Инструменты дизайнера: render_visual и finish."""
import json
import re

from core.agent import Ctx, image_block
from core import figma
from core.render import render, templates_index


def system_extra() -> str:
    from core import config
    idx = {k: ({"size": v["size"], "use": v["use"]} if not k.startswith("_") else v) for k, v in templates_index().items()}
    return ("# Шаблоны\n```json\n" + json.dumps(idx, ensure_ascii=False, indent=1) + "\n```\n\n"
            + figma.system_note())


TOOLS = [
    {
        "name": "render_visual",
        "description": "Отрендерить визуал по шаблону. Возвращает картинку и список проблем вёрстки "
                       "(пустой список = всё чисто). Повторный вызов с тем же name перезаписывает файл.",
        "input_schema": {
            "type": "object",
            "properties": {
                "template": {"type": "string", "enum": [k for k in templates_index() if not k.startswith("_")]},
                "name": {"type": "string", "description": "короткое имя файла латиницей, напр. onboarding-tg"},
                "data": {
                    "type": "object",
                    "description": "Поля шаблона — см. «_fields» в списке шаблонов. Обязателен title. Для article-steps — items, для article-compare — table.",
                    "properties": {
                        "title": {"type": "string"}, "subtitle": {"type": "string"},
                        "rubric": {"type": "string"}, "date": {"type": "string"},
                        "author": {"type": "string"}, "author_photo": {"type": "string"},
                        "layout": {"type": "string", "enum": ["graphic-top", "info-top", "info-left", "info-right"]},
                        "graphic": {"type": "string", "enum": ["mix", "blocks", "frame", "bars", "hearts"]},
                        "palette": {"type": "string", "enum": ["auto", "green", "lime", "blue", "cyan", "violet", "pink", "orange", "red", "yellow", "black"]},
                        "motifs": {"type": "array", "items": {"type": "string"}},
                        "image": {"type": "string"},
                        "image_shape": {"type": "string", "enum": ["cutout", "circle", "rounded"]},
                        "tag_color": {"type": "string"},
                        "format": {"type": "string", "enum": ["16:9", "4:5", "square", "story"]},
                        "images": {"type": "array", "items": {"type": "string"}},
                        "caption": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "badges": {"type": "array", "items": {"type": "object", "properties": {
                            "image": {"type": "string"}, "text": {"type": "string"}, "label": {"type": "string"}}}},
                        "show_logo": {"type": "boolean", "description": "логотип «дизайнерс» по умолчанию НЕ ставим; true — только если в задаче прямо попросили логотип"},
                        "items": {"type": "array", "items": {"type": "string"}},
                        "table": {"type": "object", "properties": {
                            "columns": {"type": "array", "items": {"type": "string"}},
                            "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}}},
                    },
                },
            },
            "required": ["template", "name", "data"],
        },
    },
    {
        "name": "finish",
        "description": "Сдать работу владельцу на согласование.",
        "input_schema": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"}, "description": "имена из render_visual"},
                "caption": {"type": "string", "description": "что сделал и почему, 1–3 предложения"},
            },
            "required": ["files", "caption"],
        },
    },
]


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-")[:40] or "visual"


TOOLS += figma.tools()


async def handle(name: str, inp: dict, ctx: Ctx):
    if name in figma.HANDLERS:
        return await figma.HANDLERS[name](inp, ctx)
    if name == "render_visual":
        fname = _safe_name(inp["name"])
        png = ctx.job_dir / f"{fname}.png"
        if ctx.progress:
            await ctx.progress(f"рендерю {inp['template']}: {inp['data'].get('title', '')[:40]}")
        issues = await render(inp["template"], inp.get("data", {}), png)
        png.with_suffix(".json").write_text(json.dumps({"template": inp["template"], "data": inp.get("data", {})},
                                                       ensure_ascii=False), "utf-8")
        text = ("Проблем нет." if not issues else "Найдены проблемы:\n- " + "\n- ".join(issues))
        return [{"type": "text", "text": f"Файл: {fname}\n{text}"}, image_block(png)]

    if name == "finish":
        files = []
        for f in inp.get("files", []):
            p = ctx.job_dir / f"{_safe_name(f.removesuffix('.png'))}.png"
            if p.exists():
                files.append(p)
        if not files:
            return [{"type": "text", "text": "Ни одного из этих файлов нет — сначала render_visual."}]
        ctx.files, ctx.caption, ctx.done = files, inp.get("caption", ""), True
        return [{"type": "text", "text": "Сдано на согласование."}]

    return [{"type": "text", "text": f"Нет инструмента {name}"}]
