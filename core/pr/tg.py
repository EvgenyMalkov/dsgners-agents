"""Команды PR-агента в боте и кнопки 👍/👎 под карточками дайджеста.

Регистрируются в bot.py сразу после создания Dispatcher — раньше общего обработчика текста.
"""
import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from core import config
from core.pr import jobs, store

_bot: Bot | None = None
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
KINDS = ("habr", "vc", "rss")


def _owner(x) -> bool:
    return x.from_user and x.from_user.id in config.OWNER_IDS


def vote_kb(aid: int, chosen: int | None = None) -> InlineKeyboardMarkup:
    up = ("✅ 👍" if chosen == 1 else "👍")
    down = ("✅ 👎" if chosen == -1 else "👎")
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=up, callback_data=f"pr:v:{aid}:1"),
                                                  InlineKeyboardButton(text=down, callback_data=f"pr:v:{aid}:-1")]])


async def send(text: str, aid: int | None = None):
    if not _bot:
        return
    for uid in config.OWNER_IDS:
        await _bot.send_message(uid, text[:4000], link_preview_options=NO_PREVIEW,
                                reply_markup=vote_kb(aid) if aid else None)


def setup(bot: Bot):
    global _bot
    _bot = bot
    jobs.setup(send)


async def pr_scan(m: Message):
    if not _owner(m):
        return
    await m.answer("🤝 Собираю и оцениваю статьи — это несколько минут. Потом пришлю дайджест.")

    async def go():
        note = await jobs.run("scan")
        await m.answer(f"🤝 Сбор: {note}")
        await m.answer(f"🤝 Дайджест: {await jobs.run('digest')}")
    asyncio.create_task(go())


async def pr_digest(m: Message):
    if not _owner(m):
        return
    await m.answer(f"🤝 Дайджест: {await jobs.run('digest')}")


async def pr_test(m: Message):
    if not _owner(m):
        return
    await m.answer("🧪 Проверяю источники — около минуты.")
    await m.answer(await jobs.test(), link_preview_options=NO_PREVIEW)


async def pr_status(m: Message):
    if _owner(m):
        await m.answer(jobs.status(), link_preview_options=NO_PREVIEW)


SRC_HELP = ("Источники PR:\n"
            "/pr_src list — список\n"
            "/pr_src add habr|vc|rss адрес-RSS [название]\n"
            "/pr_src off N · /pr_src on N · /pr_src del N\n\n"
            "habr — RSS хаба Хабра, например https://habr.com/ru/rss/hubs/design/articles/?fl=ru\n"
            "vc — RSS vc.ru; rss — любой другой RSS (детали берутся со страницы статьи)")


async def pr_src(m: Message):
    if not _owner(m):
        return
    parts = (m.text or "").split(maxsplit=4)
    cmd = parts[1].lower() if len(parts) > 1 else "list"
    if cmd == "list":
        lines = [f"{'✅' if s['enabled'] else '⏸'} #{s['id']} [{s['kind']}] {s['title']}\n   {s['url']}"
                 for s in store.sources()]
        await m.answer("\n".join(lines) + "\n\n" + SRC_HELP, link_preview_options=NO_PREVIEW)
    elif cmd == "add":
        kind = parts[2].lower() if len(parts) > 2 else ""
        url = parts[3] if len(parts) > 3 else ""
        if kind not in KINDS or not url.startswith("http"):
            await m.answer(SRC_HELP)
            return
        sid = store.add_source(kind, url, parts[4] if len(parts) > 4 else url)
        await m.answer(f"Добавил #{sid}. Проверь, что читается: /pr_test" if sid else "Такой адрес уже есть.")
    elif cmd in ("on", "off", "del") and len(parts) >= 3 and parts[2].lstrip("#").isdigit():
        sid = int(parts[2].lstrip("#"))
        if cmd == "del":
            ok = store.del_source(sid)
        else:
            store.set_source(sid, enabled=int(cmd == "on"))
            ok = True
        await m.answer("Готово." if ok else "Нет такого источника.")
    else:
        await m.answer(SRC_HELP)


async def pr_vote(c: CallbackQuery):
    if not _owner(c):
        return
    _, _, aid, v = c.data.split(":")
    r = jobs.vote(int(aid), int(v))
    if not r:
        await c.answer("Статья не найдена")
        return
    try:
        await c.message.edit_reply_markup(reply_markup=vote_kb(int(aid), int(v)))
    except Exception:
        pass
    await c.answer("Учёл 👍" if v == "1" else "Учёл 👎 — такие буду оценивать ниже")


def register(dp: Dispatcher):
    dp.message.register(pr_scan, Command("pr_scan"))
    dp.message.register(pr_digest, Command("pr_digest"))
    dp.message.register(pr_test, Command("pr_test"))
    dp.message.register(pr_status, Command("pr_status"))
    dp.message.register(pr_src, Command("pr_src"))
    dp.callback_query.register(pr_vote, F.data.regexp(r"^pr:v:\d+:-?1$"))
