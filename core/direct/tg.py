"""Команды Директ-агента в боте и кнопки под предложениями.

/direct_status · /direct_report [week] · /direct_funnel · /direct_places · /direct_exclude 1,3 · /direct_keep 2,5 · /direct_sync
Кнопки под списком мусорных площадок: «🚫 Исключить все» · «Не трогать эти».
Регистрируется в bot.py сразу после создания Dispatcher — раньше общего обработчика текста.
"""
import asyncio
import datetime as dt

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from core import config
from core.direct import api, jobs

_bot: Bot | None = None
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _owner(x) -> bool:
    return bool(x.from_user) and x.from_user.id in config.OWNER_IDS


def _kb(buttons) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
                                                 for row in buttons])


async def send(text: str, buttons=None):
    if not _bot:
        return
    for uid in config.OWNER_IDS:
        for i in range(0, len(text), 4000):     # длинный отчёт — несколькими сообщениями
            last = i + 4000 >= len(text)
            await _bot.send_message(uid, text[i:i + 4000], link_preview_options=NO_PREVIEW,
                                    reply_markup=_kb(buttons) if last else None)


def setup(bot: Bot):
    global _bot
    _bot = bot
    jobs.setup(send)


def _need_token(m: Message) -> bool:
    if api.enabled():
        return False
    asyncio.create_task(m.answer(jobs.status()))
    return True


async def _bg(m: Message, coro, prefix: str):
    try:
        await m.answer(prefix + str(await coro), link_preview_options=NO_PREVIEW)
    except Exception as e:
        await m.answer(f"⚠️ {e}")


def _nums(text: str) -> list[int]:
    arg = ((text or "").split(maxsplit=1) + [""])[1]
    return [int(x) for x in arg.replace(" ", ",").split(",") if x.strip().isdigit()]


# ---------- команды ----------

async def direct_status(m: Message):
    if _owner(m):
        await m.answer(jobs.status(), link_preview_options=NO_PREVIEW)


async def direct_report(m: Message):
    if not _owner(m) or _need_token(m):
        return
    week = "week" in (m.text or "").lower() or "недел" in (m.text or "").lower()
    await m.answer("📈 Собираю статистику Директа и Метрики — до пары минут.")
    asyncio.create_task(_bg(m, jobs.run("weekly" if week else "daily"), "📈 Готово: "))


async def direct_sync(m: Message):
    if not _owner(m) or _need_token(m):
        return
    await m.answer("📈 Обновляю кампании, объявления и статистику за 3 дня.")

    async def go():
        changes, rejected, note = await jobs.collect(3)
        await jobs._alert_changes(changes, rejected)
        return note
    asyncio.create_task(_bg(m, go(), "📈 Обновил: "))


async def direct_funnel(m: Message):
    if not _owner(m):
        return
    end = jobs._today() - dt.timedelta(days=1)
    await m.answer("🔻 Считаю воронку за 7 дней…")
    asyncio.create_task(_bg(m, jobs.funnel_text(end - dt.timedelta(days=6), end), ""))


async def direct_places(m: Message):
    if not _owner(m) or _need_token(m):
        return
    rows, batch = jobs.propose_places()
    await m.answer(jobs.places_text(rows), link_preview_options=NO_PREVIEW, reply_markup=_kb(jobs.place_buttons(rows, batch)))


def _pick(m: Message) -> list[str]:
    items = jobs.proposal_items()
    return [items[i - 1] for i in _nums(m.text) if 0 < i <= len(items)]


async def direct_exclude(m: Message):
    if not _owner(m):
        return
    pick = _pick(m)
    if not pick:
        await m.answer("Номера из последнего списка /direct_places, например: /direct_exclude 1,3")
        return
    await m.answer(f"🚫 Исключаю {len(pick)}: {', '.join(pick)}")
    asyncio.create_task(_bg(m, jobs.exclude(pick), ""))


async def direct_keep(m: Message):
    if not _owner(m):
        return
    pick = _pick(m)
    if not pick:
        await m.answer("Номера из последнего списка /direct_places, например: /direct_keep 2,5")
        return
    jobs.keep(pick)
    await m.answer(f"Не трогаю и больше не предлагаю: {', '.join(pick)}")


# ---------- кнопки ----------

async def direct_cb(c: CallbackQuery):
    if not _owner(c):
        return
    _, act, batch = c.data.split(":")
    items = jobs.proposal_items(int(batch))
    if not items:
        await c.answer("Список устарел — открой свежий: /direct_places", show_alert=True)
        return
    await c.message.edit_reply_markup(reply_markup=None)
    if act == "xa":
        await c.answer("Исключаю…")
        await c.message.answer(f"🚫 Исключаю площадок: {len(items)}…")
        await c.message.answer(await jobs.exclude(items))
    elif act == "ka":
        jobs.keep(items)
        await c.answer("Ок, не трогаю")
        await c.message.answer(f"Оставил {len(items)} площадок и больше их не предлагаю.")


def register(dp: Dispatcher):
    for name, fn in (("direct_status", direct_status), ("direct_report", direct_report), ("direct_sync", direct_sync),
                     ("direct_funnel", direct_funnel), ("direct_places", direct_places),
                     ("direct_exclude", direct_exclude), ("direct_keep", direct_keep)):
        dp.message.register(fn, Command(name))
    dp.callback_query.register(direct_cb, F.data.regexp(r"^dr:(xa|ka):\d+$"))
