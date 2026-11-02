"""Telegram-бот команды: принимает задачи, показывает результат, кнопки «Ок / Переделать».

Задача по умолчанию идёт дизайнеру. Позже: /smm ..., /seo ...
Отвечает только владельцам из OWNER_IDS.
"""
import asyncio
import logging
import shutil
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, Message)

from core import config, feedback, spend
from core.agent import run_agent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
dp = Dispatcher()
JOBS: dict[str, dict] = {}        # job_id -> {agent, task, files}
WAIT_REASON: dict[int, str] = {}  # user_id -> job_id, ждём причину переделки
AGENT_TITLES = {"designer": "🎨 Дизайнер"}

HELP = (
    "Команда dsgners на связи.\n\n"
    "Просто напиши задачу — её возьмёт дизайнер. Например:\n"
    "«Обложка для поста: как мы переделали онбординг, рост конверсии 34%. Телега и сторис»\n\n"
    "Можно прислать картинку (работу автора) с подписью-задачей.\n\n"
    "/rules — правила, которые дизайнер выучил из твоих правок\n"
    "/spend — расходы на API за месяц"
)


def is_owner(m) -> bool:
    return bool(m.from_user) and m.from_user.id in config.OWNER_IDS


@dp.message(CommandStart())
async def start(m: Message):
    if not is_owner(m):
        await m.answer(f"Твой Telegram ID: {m.from_user.id}\nВпиши его в OWNER_IDS в файле .env на сервере и перезапусти бота.")
        return
    await m.answer(HELP)


@dp.message(Command("help"))
async def help_(m: Message):
    if is_owner(m):
        await m.answer(HELP)


@dp.message(Command("rules"))
async def rules(m: Message):
    if not is_owner(m):
        return
    txt = feedback.read("designer").strip() or "Пока пусто — правила появятся после первой «Переделать»."
    await m.answer(txt[-3800:])


@dp.message(Command("spend"))
async def spend_cmd(m: Message):
    if not is_owner(m):
        return
    parts = [f"{AGENT_TITLES.get(a, a)}: ${v:.2f}" for a, v in spend.by_agent_this_month().items()]
    await m.answer(f"Расход за месяц: ${spend.this_month():.2f} из ${config.MONTHLY_LIMIT_USD:.0f}\n" + "\n".join(parts))


@dp.message(F.photo | F.document)
async def with_image(m: Message, bot: Bot):
    if not is_owner(m):
        return
    if m.document and not (m.document.mime_type or "").startswith("image/"):
        await m.answer("Пока понимаю только картинки.")
        return
    file_id = m.photo[-1].file_id if m.photo else m.document.file_id
    ext = ".jpg" if m.photo else ("." + (m.document.file_name or "x.png").rsplit(".", 1)[-1])
    dest = config.DATA / "uploads" / f"{uuid.uuid4().hex[:10]}{ext}"
    await bot.download(file_id, destination=dest)
    task = (m.caption or "Сделай визуал с этой работой") + f"\n\nПрикреплённая картинка: {dest}"
    await launch(m, "designer", task)


@dp.message(F.text)
async def text_task(m: Message):
    if not is_owner(m):
        return
    uid = m.from_user.id
    if uid in WAIT_REASON:
        job = JOBS.get(WAIT_REASON.pop(uid))
        if job:
            feedback.add_rule(job["agent"], m.text, job["task"])
            await m.answer("Записал в правила. Переделываю…")
            redo_task = f"{job['task']}\n\nПредыдущий вариант отклонён владельцем. Причина: {m.text}"
            await launch(m, job["agent"], redo_task, original=job["task"])
            return
    await launch(m, "designer", m.text)


async def launch(m: Message, agent: str, task: str, original: str | None = None):
    title = AGENT_TITLES.get(agent, agent)
    status = await m.answer(f"{title} взялся за задачу…")

    async def progress(txt: str):
        try:
            await status.edit_text(f"{title}: {txt}…")
        except Exception:
            pass

    try:
        res = await run_agent(agent, task, progress=progress)
    except Exception as e:
        logging.exception("agent failed")
        await status.edit_text(f"⚠️ {title}: ошибка — {e}")
        return

    if not res["files"]:
        await status.edit_text(f"{title} ничего не сдал:\n{res['text'][:3500]}")
        return

    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"agent": agent, "task": original or task, "files": res["files"]}
    files = res["files"][:10]
    if len(files) == 1:
        await m.answer_photo(FSInputFile(files[0]))
    else:
        await m.answer_media_group([InputMediaPhoto(media=FSInputFile(f)) for f in files])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Ок", callback_data=f"ok:{job_id}"),
        InlineKeyboardButton(text="🔁 Переделать", callback_data=f"redo:{job_id}"),
    ]])
    await m.answer(f"{res['text'][:3500]}\n\n💸 ${res['cost']:.3f}", reply_markup=kb)
    try:
        await status.delete()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("ok:"))
async def approve(c: CallbackQuery):
    if not is_owner(c):
        return
    job = JOBS.get(c.data.split(":", 1)[1])
    if not job:
        await c.answer("Задача устарела (бот перезапускался)")
        return
    approved = config.OUTBOX / "approved"
    approved.mkdir(exist_ok=True)
    for f in job["files"]:
        shutil.copy(f, approved / f"{f.parent.name}-{f.name}")
        await c.message.answer_document(FSInputFile(f), caption="Исходник без сжатия")
    await c.message.edit_reply_markup(reply_markup=None)
    await c.answer("Принято 👍")


@dp.callback_query(F.data.startswith("redo:"))
async def redo(c: CallbackQuery):
    if not is_owner(c):
        return
    job_id = c.data.split(":", 1)[1]
    if job_id not in JOBS:
        await c.answer("Задача устарела (бот перезапускался)")
        return
    WAIT_REASON[c.from_user.id] = job_id
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("Что не так? Напиши одним сообщением — это станет правилом, и я переделаю.")
    await c.answer()


async def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN в .env")
    bot = Bot(config.TELEGRAM_BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
