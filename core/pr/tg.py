"""Команды PR-агента в боте и кнопки под карточками.

Карточка статьи: 👍/👎 (калибровка) + для черновика ✅ Отправить · 🔁 Другой вариант · ✏️ Правки · ⏭ Пропустить · 🚫 Не писать.
Регистрируется в bot.py сразу после создания Dispatcher — раньше общего обработчика текста.
"""
import asyncio

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message

from core import config
from core.pr import jobs, sender, store

_bot: Bot | None = None
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
KINDS = ("habr", "vc", "rss")
WAIT_EDIT: dict[int, tuple[int, int, int]] = {}   # user_id -> (outreach_id, chat_id, message_id карточки)


def _owner(x) -> bool:
    return bool(x.from_user) and x.from_user.id in config.OWNER_IDS


def _b(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _draft_rows(o: dict) -> list:
    if not o:
        return []
    i = o["id"]
    if o["status"] in ("draft", "failed"):
        send = _b("📋 Отправил вручную", f"pr:m:{i}") if o["channel"] == "vk" else _b("✅ Отправить", f"pr:s:{i}")
        return [[send, _b("🔁 Другой вариант", f"pr:r:{i}")],
                [_b("✏️ Правки", f"pr:e:{i}"), _b("⏭ Пропустить", f"pr:k:{i}"), _b("🚫 Не писать", f"pr:x:{i}")]]
    if o["status"] == "approved":
        return [[_b("↩️ Отменить отправку", f"pr:u:{i}")]]
    return []


def kb(aid: int | None = None, oid: int | None = None) -> InlineKeyboardMarkup | None:
    rows = []
    if aid:
        a = store.article(aid) or {}
        v = a.get("vote")
        rows.append([_b("✅ 👍" if v == 1 else "👍", f"pr:v:{aid}:1"), _b("✅ 👎" if v == -1 else "👎", f"pr:v:{aid}:-1")])
        o = next((x for x in reversed(store.outreach_for_article(aid)) if x["kind"] == "first"), None)
        rows += _draft_rows(o)
    if oid:
        rows += _draft_rows(store.outreach(oid))
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _text(aid: int | None, oid: int | None) -> str:
    if oid and not aid:
        return jobs.followup_card(store.outreach(oid))
    return jobs.card(store.article(aid))


async def send(text: str, aid: int | None = None, oid: int | None = None):
    if not _bot:
        return
    for uid in config.OWNER_IDS:
        await _bot.send_message(uid, text[:4000], link_preview_options=NO_PREVIEW, reply_markup=kb(aid, oid))


def setup(bot: Bot):
    global _bot
    _bot = bot
    jobs.setup(send)


async def _refresh(msg, aid: int | None, oid: int | None):
    try:
        await msg.edit_text(_text(aid, oid)[:4000], link_preview_options=NO_PREVIEW, reply_markup=kb(aid, oid))
    except Exception:
        pass   # «message is not modified»


# ---------- команды ----------

async def pr_scan(m: Message):
    if not _owner(m):
        return
    await m.answer("🤝 Собираю и оцениваю статьи — это несколько минут. Потом пришлю дайджест.")

    async def go():
        await m.answer(f"🤝 Сбор: {await jobs.run('scan')}")
        await m.answer(f"🤝 Дайджест: {await jobs.run('digest')}")
    asyncio.create_task(go())


async def pr_digest(m: Message):
    if not _owner(m):
        return
    await m.answer("🤝 Готовлю дайджест: ищу контакты и пишу черновики — пара минут.")
    asyncio.create_task(_say_after(m, jobs.run("digest"), "🤝 Дайджест: "))


async def _say_after(m: Message, coro, prefix: str):
    await m.answer(prefix + str(await coro), link_preview_options=NO_PREVIEW)


async def pr_test(m: Message):
    if not _owner(m):
        return
    await m.answer("🧪 Проверяю источники — около минуты.")
    await m.answer(await jobs.test(), link_preview_options=NO_PREVIEW)


async def pr_status(m: Message):
    if _owner(m):
        await m.answer(jobs.status(), link_preview_options=NO_PREVIEW)


async def pr_report(m: Message):
    if _owner(m):
        await jobs.run("weekly")


async def pr_author(m: Message):
    if not _owner(m):
        return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().startswith("http"):
        await m.answer("Пришли так: /pr_author https://habr.com/ru/articles/…")
        return
    await m.answer("🤝 Разбираю статью: оценка, контакты, черновик — минута.")
    asyncio.create_task(_say_after(m, jobs.one(parts[1]), "🤝 "))


async def pr_outreach(m: Message):
    if not _owner(m):
        return
    arg = ((m.text or "").split() + [""])[1].lower()
    if arg in ("on", "off"):
        jobs.set_outreach_on(arg == "on")
    await m.answer(f"Черновики сообщений в дайджесте: {'включены' if jobs.outreach_on() else 'выключены'}. "
                   "Переключить: /pr_outreach on|off")


async def pr_pause(m: Message):
    if _owner(m):
        sender.pause("вручную")
        await m.answer("⏸ Отправка на паузе. Одобренные сообщения ждут в очереди. Вернуть: /pr_resume")


async def pr_resume(m: Message):
    if _owner(m):
        sender.resume()
        await m.answer(f"▶️ Отправка включена. {jobs.sender_line()}")


async def pr_stop(m: Message):
    if not _owner(m):
        return
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await m.answer("Пришли так: /pr_stop @username (или e-mail, или ссылку на профиль автора)")
        return
    key = parts[1].strip().lstrip("@")
    store.stop(key, "владелец: /pr_stop")
    n = 0
    for o in store.outreach_by("approved", 500) + store.outreach_by("draft", 500):
        if o["value"].lower() == key.lower():
            store.set_outreach(o["id"], status="stopped")
            n += 1
    await m.answer(f"🚫 {key} в стоп-листе навсегда." + (f" Снял из очереди: {n}." if n else ""))


async def pr_login(m: Message):
    if not _owner(m):
        return
    await m.answer(f"Аккаунт отправки: {jobs.sender_line()}\n\n"
                   "Чтобы подключить аккаунт менеджера, на сервере (один раз):\n"
                   "sudo -u agents /home/agents/dsgners-agents/.venv/bin/python -m core.pr.login\n"
                   "systemctl restart dsgners-bot\n\n"
                   "Скрипт спросит номер менеджера, код из Telegram и облачный пароль. Код никому не пересылай.")


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


# ---------- кнопки ----------

async def pr_vote(c: CallbackQuery):
    if not _owner(c):
        return
    _, _, aid, v = c.data.split(":")
    if not jobs.vote(int(aid), int(v)):
        await c.answer("Статья не найдена")
        return
    try:
        await c.message.edit_reply_markup(reply_markup=kb(int(aid)))
    except Exception:
        pass
    await c.answer("Учёл 👍" if v == "1" else "Учёл 👎 — такие буду оценивать ниже")


def _ids(o: dict) -> tuple[int | None, int | None]:
    """Карточка статьи (aid) или карточка напоминания (oid)."""
    return (None, o["id"]) if o["kind"] == "followup" else (o["article_id"], None)


async def pr_action(c: CallbackQuery):
    if not _owner(c):
        return
    _, act, oid = c.data.split(":")
    oid = int(oid)
    o = store.outreach(oid)
    if not o:
        await c.answer("Сообщение не найдено")
        return
    aid, foid = _ids(o)
    if act == "s":
        o = jobs.approve(oid)
        msg = "В очереди. Уйдёт в окно отправки" if o["status"] == "approved" else "Уже не черновик"
        if not sender.ready() and o["channel"] == "tg":
            msg += " — но аккаунт менеджера не подключён (/pr_login)"
        await c.answer(msg, show_alert=not sender.ready())
    elif act == "u":
        if o["status"] == "approved":
            store.set_outreach(oid, status="draft")
        await c.answer("Вернул в черновик")
    elif act == "r":
        await c.answer("Пишу другой вариант…")
        await jobs.redraft(oid)
    elif act == "e":
        WAIT_EDIT[c.from_user.id] = (oid, c.message.chat.id, c.message.message_id)
        await c.message.answer("✏️ Что поправить в сообщении? Напиши одним сообщением — перепишу и запомню как правило.")
        await c.answer()
        return
    elif act == "k":
        jobs.skip(oid)
        await c.answer("Пропустил")
    elif act == "x":
        jobs.stop_forever(oid)
        await c.answer("Больше никогда не пишем этому автору")
    elif act == "m":
        jobs.manual_sent(oid)
        await c.answer("Отметил как отправленное")
    await _refresh(c.message, aid, foid)


async def pr_edit_text(m: Message):
    oid, chat, mid = WAIT_EDIT.pop(m.from_user.id)
    await m.answer("Переписываю…")
    o = await jobs.redraft(oid, comment=m.text or "")
    aid, foid = _ids(o)
    try:
        await _bot.edit_message_text(_text(aid, foid)[:4000], chat_id=chat, message_id=mid,
                                     link_preview_options=NO_PREVIEW, reply_markup=kb(aid, foid))
        await m.answer("Готово — обновил карточку выше. Правило запомнил.")
    except Exception:
        await m.answer(_text(aid, foid)[:4000], link_preview_options=NO_PREVIEW, reply_markup=kb(aid, foid))


def register(dp: Dispatcher):
    for name, fn in (("pr_scan", pr_scan), ("pr_digest", pr_digest), ("pr_test", pr_test), ("pr_status", pr_status),
                     ("pr_src", pr_src), ("pr_report", pr_report), ("pr_author", pr_author), ("pr_outreach", pr_outreach),
                     ("pr_pause", pr_pause), ("pr_resume", pr_resume), ("pr_stop", pr_stop), ("pr_login", pr_login)):
        dp.message.register(fn, Command(name))
    dp.message.register(pr_edit_text, F.text, lambda m: bool(m.from_user) and m.from_user.id in WAIT_EDIT
                        and not (m.text or "").startswith("/"))
    dp.callback_query.register(pr_vote, F.data.regexp(r"^pr:v:\d+:-?1$"))
    dp.callback_query.register(pr_action, F.data.regexp(r"^pr:[surekxm]:\d+$"))
