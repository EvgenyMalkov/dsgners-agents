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
        "name": "inspect_page",
        "description": "Посмотреть HTML-прототип или страницу сайта для ролика (для экранов из Figma не нужен): карта элементов (селекторы, координаты, подписи кнопок) "
                       "на мобильной ширине 375 + скриншот первого экрана. Нужен перед render_motion, чтобы написать "
                       "сценарий нажатий. Можно передать css/js, чтобы сразу проверить, как страница выглядит после правок.",
        "input_schema": {"type": "object", "properties": {
            "source": {"type": "string", "description": "путь к HTML-прототипу (из задачи) или адрес страницы сайта"},
            "css": {"type": "string"}, "js": {"type": "string"},
        }, "required": ["source"]},
    },
    {
        "name": "render_motion",
        "description": "Ролик (MP4 + GIF): живой интерфейс в мокапе телефона на фирменной подложке. Сначала прочитай навык "
                       "«Ролики и анимации» (use_skill). Возвращает лист из 6 кадров и замечания (пусто = чисто).",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string", "description": "имя файла латиницей"},
            "source": {"type": "string", "description": "HTML-прототип (путь) или адрес страницы — если фича интерактивная"},
            "screens": {"type": "array", "items": {"type": "string"}, "description": "ИЛИ экраны из Figma (пути PNG из figma_export, "
                        "full: true для длинных): прокрутка ленты/страницы, переходы между экранами. Ширина экрана — 375"},
            "fixed_top": {"type": "integer", "description": "для screens: сколько px сверху экрана закрепить при прокрутке (шапка, обычно 64)"},
            "overlays": {"type": "array", "description": "для screens: закреплённые элементы поверх (навбар снизу и т.п.) — "
                         "[{image: путь PNG из figma_export, pos: bottom|top, offset: px, width: px в CSS}]", "items": {"type": "object"}},
            "format": {"type": "string", "enum": ["4:5", "1:1", "9:16", "16:9"], "description": "по умолчанию 4:5 (Telegram)"},
            "theme": {"type": "string", "enum": ["light", "dark"]},
            "header": {"type": "boolean", "description": "вставить реальную мобильную шапку сайта (для прототипов по умолчанию да)"},
            "article": {"type": "object", "description": "верх статьи как на сайте вокруг фичи",
                        "properties": {"author": {"type": "string"}, "title": {"type": "string"},
                                       "text": {"type": "array", "items": {"type": "string"}},
                                       "cover": {"type": "string", "description": "путь к обложке (можно отрендерить article-cover)"},
                                       "before": {"type": "string", "description": "селектор: вставить статью перед этим элементом"}}},
            "css": {"type": "string", "description": "CSS для страницы: спрятать лишнее из прототипа, закрепить навбар и т.п."},
            "js": {"type": "string", "description": "JS после загрузки (подготовить состояние)"},
            "zoom": {"type": "number", "description": "масштаб страницы внутри экрана (1.4 — телефон целиком, 2.5–3 — крупный план)"},
            "focus": {"type": "string", "description": "селектор элемента, который ставим в точку focus_at (напр. навбар)"},
            "focus_at": {"type": "array", "items": {"type": "number"}, "description": "[x, y] доли кадра, по умолчанию [0.5, 0.42]"},
            "caption": {"type": "string", "description": "подпись на подложке, \n — перенос; пусто — без подписи"},
            "caption_pos": {"type": "string", "enum": ["auto", "top", "bottom", "none"]},
            "palette": {"type": "string", "description": "цвет подложки из брендбука: violet, blue, pink, lime, green, cyan, orange, red, yellow"},
            "motifs": {"type": "array", "items": {"type": "string"}, "description": "шейпы бренда на подложке (2–4)"},
            "steps": {"type": "array", "description": "сценарий: {tap: селектор} | {tap_at: [x, y]} | {wait: сек} | {scroll_by: px, dur: сек} | "
                      "{scroll_to: селектор, dur} | {screen: номер, transition: slide|fade, dur} | {js: код}",
                      "items": {"type": "object"}},
            "loop": {"type": "boolean", "description": "проверять бесшовный цикл (по умолчанию да)"},
        }, "required": ["name", "steps"]},
    },
    {
        "name": "finish",
        "description": "Сдать работу владельцу на согласование.",
        "input_schema": {
            "type": "object",
            "properties": {
                "files": {"type": "array", "items": {"type": "string"}, "description": "имена из render_visual / render_motion"},
                "caption": {"type": "string", "description": "что сделал и почему, 1–3 предложения"},
            },
            "required": ["files", "caption"],
        },
    },
]


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-")[:40] or "visual"


TOOLS += figma.tools()

FEATURE_WORDS = ("на сайте", "на сайт", "фич", "онбординг", "обновлен", "запусти", "релиз", "раздел", "теперь можно",
                 "новое на", "появил", "лента", "в профиле", "на проде", "функци")
WITH_SCREENS = ("showcase", "showcase-phones", "promo", "promo-duo")


def _feature_check(inp: dict, ctx: Ctx) -> list[str]:
    """Жёсткая проверка: визуал про фичу сайта без экрана из Figma не сдаём."""
    if not figma.tools() or inp["template"] in WITH_SCREENS:
        return []
    d = inp.get("data") or {}
    if d.get("image") or d.get("images"):
        return []
    tp = ctx.job_dir / "task.md"
    task = (tp.read_text("utf-8") if tp.exists() else "").lower() + " " + json.dumps(d, ensure_ascii=False).lower()
    if not any(w in task for w in FEATURE_WORDS):
        return []
    return ["Это визуал про фичу/обновление сайта, а экрана нет. Найди экраны в Figma (figma_find в dev-файле, "
            "figma_export 1–2 ключевых экрана; если в задаче уже есть пути к экранам — бери их) и собери promo / "
            "promo-duo / showcase. Карточку без экрана можно сдать, только если в Figma правда ничего нет — "
            "тогда напиши об этом в finish."]


async def handle(name: str, inp: dict, ctx: Ctx):
    if name in figma.HANDLERS:
        return await figma.HANDLERS[name](inp, ctx)
    if name == "inspect_page":
        from core import motion
        try:
            outline, shot = await motion.inspect(inp["source"], inp.get("css", ""), inp.get("js", ""))
        except Exception as e:
            return [{"type": "text", "text": f"Не открыл страницу: {e}"}]
        return [{"type": "text", "text": outline[:9000]}, image_block(shot, 900)]
    if name == "render_motion":
        from core import motion
        fname = _safe_name(inp["name"])
        if ctx.progress:
            await ctx.progress(f"записываю ролик {fname} (пара минут)")
        try:
            res = await motion.record(dict(inp), ctx.job_dir, fname, progress=ctx.progress)
        except Exception as e:
            return [{"type": "text", "text": f"Ролик не записался: {e}"}]
        (ctx.job_dir / f"{fname}.motion.json").write_text(json.dumps(inp, ensure_ascii=False), "utf-8")
        if not res.get("mp4"):
            return [{"type": "text", "text": "Ролик не записался:\n- " + "\n- ".join(res.get("issues") or ["?"])}]
        iss = res["issues"]
        text = (f"Файл: {fname} (mp4 + gif), {res['seconds']} с. "
                + ("Проблем нет." if not iss else "Найдены проблемы:\n- " + "\n- ".join(iss)))
        return [{"type": "text", "text": text + "\nНиже 6 моментов ролика — проверь крупность, подложку, подпись, цикл."},
                image_block(res["sheet"], 1100)]
    if name == "render_visual":
        fname = _safe_name(inp["name"])
        png = ctx.job_dir / f"{fname}.png"
        if ctx.progress:
            await ctx.progress(f"рендерю {inp['template']}: {inp['data'].get('title', '')[:40]}")
        issues = await render(inp["template"], inp.get("data", {}), png)
        png.with_suffix(".json").write_text(json.dumps({"template": inp["template"], "data": inp.get("data", {})},
                                                       ensure_ascii=False), "utf-8")
        fi = _feature_check(inp, ctx)
        blocked = ctx.meta.setdefault("feature_blocked", [])
        if fi and fname not in blocked:
            blocked.append(fname)
        elif not fi and fname in blocked:
            blocked.remove(fname)
        issues += fi
        text = ("Проблем нет." if not issues else "Найдены проблемы:\n- " + "\n- ".join(issues))
        return [{"type": "text", "text": f"Файл: {fname}\n{text}"}, image_block(png)]

    if name == "finish":
        files = []
        for f in inp.get("files", []):
            stem = _safe_name(re.sub(r"\.(png|mp4|gif)$", "", f))
            for ext in (".png", ".mp4", ".gif"):
                p = ctx.job_dir / f"{stem}{ext}"
                if p.exists() and p not in files:
                    files.append(p)
        if not files:
            return [{"type": "text", "text": "Ни одного из этих файлов нет — сначала render_visual."}]
        bad = [p.stem for p in files if p.stem in ctx.meta.get("feature_blocked", [])]
        if bad and ctx.meta.get("figma_empty", 0) < 2:
            return [{"type": "text", "text": "Не сдаю: " + ", ".join(bad) + " — визуал про фичу сайта без экрана. "
                     "Это правило важнее любых «как в прошлый раз» и «те же параметры». Возьми экраны (из задачи или "
                     "figma_find → figma_export) и собери promo / promo-duo / showcase."}]
        ctx.files, ctx.caption, ctx.done = files, inp.get("caption", ""), True
        return [{"type": "text", "text": "Сдано на согласование."}]

    return [{"type": "text", "text": f"Нет инструмента {name}"}]
