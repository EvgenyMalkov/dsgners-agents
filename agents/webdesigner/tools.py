"""Инструменты веб-дизайнера: макеты страниц и блоков сайта (HTML → картинка), чтение сайта, документы."""
import json

from core import common_tools as ct
from core import figma
from core.agent import Ctx, image_block
from core.render import render_html


def system_extra() -> str:
    from core import brandgfx, config
    ds = (config.TEAM / "design-system.json").read_text("utf-8")
    logo = (config.TEAM / "assets" / "logo.svg").read_text("utf-8")
    return ("# Фирменная дизайн-система (токены)\n```json\n" + ds + "\n```\n"
            "Фирменный шрифт подключай в <head> макета ровно этим кодом, а в CSS пиши font-family:'CoFo Sona':\n"
            "```html\n" + brandgfx.font_css() + "\n```\n"
            "Логотип (inline SVG, цвет — currentColor):\n```html\n" + logo + "\n```\n\n" + figma.system_note())


TOOLS = [
    {
        "name": "render_mockup",
        "description": "Отрендерить макет: полный HTML-документ (свой CSS внутри, без внешних JS-библиотек). "
                       "Возвращает картинку и найденные проблемы вёрстки (горизонтальный скролл, вылезающий текст, "
                       "битые картинки). Делай десктоп (1440) и мобильный (390) варианты. Тот же name перезаписывает файл.",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "имя латиницей, напр. reviews-block-desktop"},
            "html": {"type": "string"},
            "width": {"type": "integer", "enum": [1440, 1024, 390], "description": "ширина экрана"},
        }, "required": ["name", "html", "width"]},
    },
    ct.READ_PAGE,
    ct.SAVE_DOCUMENT,
    ct.ask_colleague_tool({"designer": "графический дизайнер: иллюстрации, баннеры, обложки в фирменном стиле"}),
    ct.FINISH,
] + figma.tools()


async def handle(name: str, inp: dict, ctx: Ctx):
    if name == "render_mockup":
        fname = ct.safe_name(inp["name"], "mockup")
        if ctx.progress:
            await ctx.progress(f"рендерю макет {fname} ({inp.get('width', 1440)}px)")
        png = ctx.job_dir / f"{fname}.png"
        issues, first = await render_html(inp["html"], png, int(inp.get("width", 1440)))
        txt = "Проблем нет." if not issues else "Найдены проблемы:\n- " + "\n- ".join(issues)
        return [{"type": "text", "text": f"Файл: {fname}.png (+ {fname}.html для разработки)\n{txt}\nПервый экран:"},
                image_block(first, 1100), {"type": "text", "text": "Вся страница:"}, image_block(png, 1400)]
    if name in figma.HANDLERS:
        return await figma.HANDLERS[name](inp, ctx)
    if name in ct.COMMON_HANDLERS:
        return await ct.COMMON_HANDLERS[name](inp, ctx)
    return [{"type": "text", "text": f"Нет инструмента {name}"}]
