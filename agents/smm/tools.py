"""Инструменты SMM-щика: черновики постов для Telegram-каналов, визуалы через дизайнера, чтение статей.
Публикация происходит ТОЛЬКО после «✅ Ок» владельца (см. bot.py → publish_posts)."""
import json
from pathlib import Path

from core import common_tools as ct
from core import figma
from core import config
from core.agent import Ctx

TG_CAPTION_LIMIT = 1024
TG_TEXT_LIMIT = 4096

def system_extra() -> str:
    return figma.system_note()


TOOLS = [
    {
        "name": "draft_variants",
        "description": "Сдать варианты текста поста владельцу на выбор (основной способ работы). Обычно все три стиля: "
                       "strict — строгий, деловой; normal — обычный, дружелюбный; bar — очень свойский, как другу в баре "
                       "(разговорно, с юмором, но без мата и оскорблений). Если владелец просил переписать один вариант — "
                       "передай только его. Картинки на этом шаге НЕ заказывай: дизайнер получит выбранный текст сам.",
        "input_schema": {"type": "object", "properties": {
            "strict": {"type": "string"}, "normal": {"type": "string"}, "bar": {"type": "string"},
            "image_title": {"type": "string", "description": "короткий заголовок для картинки, 2–6 слов"},
            "format": {"type": "string", "description": "формат картинки: tg-post (по умолчанию), square, story"},
            "channel": {"type": "string", "description": "канал из знаний; пусто — черновик"},
        }},
    },
    {
        "name": "draft_post",
        "description": "Добавить пост в пакет на согласование. После «Ок» владельца он уйдёт в указанный канал. "
                       "Форматирование — HTML Telegram (<b>, <i>, <u>, <s>, <a href>, <blockquote>, <code>, <tg-spoiler>), не Markdown; эмодзи — умеренно. "
                       "Канал бери только из знаний (список каналов); если канала нет — оставь пустым, пост останется черновиком.",
        "input_schema": {"type": "object", "properties": {
            "channel": {"type": "string", "description": "@username канала или его числовой id; пусто — черновик"},
            "text": {"type": "string"},
            "image": {"type": "string", "description": "имя файла картинки от коллеги (как он его назвал) или путь к присланной картинке; необязательно"},
        }, "required": ["text"]},
    },
    ct.ask_colleague_tool({"designer": "графический дизайнер: визуал к посту (обложка, сторис, квадрат) по шаблонам бренда"}),
    ct.READ_PAGE,
    ct.SAVE_DOCUMENT,
    ct.FINISH,
] + figma.tools()


def _resolve_image(value: str) -> str | None:
    if not value:
        return None
    for base in (config.OUTBOX, config.ROOT):
        p = (base / value).resolve()
        if p.exists() and p.is_file():
            return str(p)
    p = Path(value)
    return str(p) if p.is_absolute() and p.exists() else None


async def handle(name: str, inp: dict, ctx: Ctx):
    if name == "draft_variants":
        from core.smm_flow import STYLES
        from core.tg_html import clean, visible_len
        vs, notes = {}, []
        for k in STYLES:
            if (inp.get(k) or "").strip():
                vs[k], errs = clean(inp[k])
                notes += [f"{STYLES[k]}: {e}" for e in errs]
        if not vs:
            return [{"type": "text", "text": "Нужен хотя бы один вариант текста."}]
        long = [STYLES[k] for k, v in vs.items() if visible_len(v) > TG_CAPTION_LIMIT - 30]
        if long:
            return [{"type": "text", "text": f"Слишком длинно для подписи к картинке (лимит ~1000 знаков): "
                                             f"{', '.join(long)}. Сократи."}]
        ctx.meta["variants"] = vs
        ctx.meta["brief"] = {"image_title": inp.get("image_title", ""), "format": inp.get("format", ""),
                             "channel": (inp.get("channel") or "").strip(),
                             "shots": sorted(str(p) for p in ctx.job_dir.glob("figma-*.png"))}
        (ctx.job_dir / "variants.md").write_text("\n\n---\n\n".join(f"**{STYLES[k]}**\n\n{v}" for k, v in vs.items()), "utf-8")
        fix = (" Поправил разметку: " + "; ".join(notes) + ". Если смысл форматирования пострадал — сдай заново.") if notes else ""
        return [{"type": "text", "text": f"Варианты сохранены ({', '.join(STYLES[k] for k in vs)}).{fix} Теперь finish."}]
    if name == "draft_post":
        from core.tg_html import clean
        text, _ = clean(inp["text"])
        img = _resolve_image(inp.get("image", ""))
        if inp.get("image") and not img:
            return [{"type": "text", "text": f"Картинка «{inp['image']}» не найдена. Используй имя файла из ответа коллеги."}]
        limit = TG_CAPTION_LIMIT if img else TG_TEXT_LIMIT
        warn = ""
        if len(text) > TG_TEXT_LIMIT:
            return [{"type": "text", "text": f"Текст {len(text)} симв. — больше лимита Telegram {TG_TEXT_LIMIT}. Сократи."}]
        if img and len(text) > TG_CAPTION_LIMIT:
            warn = " Текст длиннее подписи к фото (1024) — уйдёт отдельным сообщением после картинки."
        if img and not (img.startswith(str(ctx.job_dir)) or any(img.endswith(f) for f in ctx.meta.get("from_colleagues", []))):
            warn += (" ВНИМАНИЕ: картинка из прошлой задачи. Если владелец просил поправить картинку или визуал — "
                     "закажи новую у дизайнера (ask_colleague) с его замечанием, старую не прикладывай.")
        posts = ctx.meta.setdefault("posts", [])
        posts.append({"channel": inp.get("channel", "").strip(), "text": text, "image": img})
        # читаемая версия для офиса
        md = "\n\n---\n\n".join(
            f"**Канал:** {p['channel'] or 'черновик (канал не указан)'}\n"
            + (f"**Картинка:** {Path(p['image']).name}\n" if p["image"] else "") + f"\n{p['text']}" for p in posts)
        (ctx.job_dir / "posts.md").write_text(md, "utf-8")
        if img and str(Path(img)).startswith(str(config.OUTBOX)):
            rel = str(Path(img).relative_to(config.OUTBOX))
            if rel not in ctx.meta.setdefault("from_colleagues", []):
                ctx.meta["from_colleagues"].append(rel)
        if ctx.progress:
            await ctx.progress(f"пост №{len(posts)} → {inp.get('channel') or 'черновик'}")
        return [{"type": "text", "text": f"Пост №{len(posts)} добавлен ({len(text)} симв.).{warn}"}]
    if name in figma.HANDLERS:
        return await figma.HANDLERS[name](inp, ctx)
    if name in ct.COMMON_HANDLERS:
        return await ct.COMMON_HANDLERS[name](inp, ctx)
    return [{"type": "text", "text": f"Нет инструмента {name}"}]
