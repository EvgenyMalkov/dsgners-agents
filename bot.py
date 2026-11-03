"""Главный процесс: Telegram-бот + веб-сервер ИИ-офиса.

Задачи из Telegram и из офиса идут через core.runner, история общая.
Готовая работа приходит владельцам в Telegram с кнопками «Ок / Переделать»
(их же можно нажать в офисе — что раньше, то и считается).
"""
import asyncio
import logging
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, Message)

from core import config, feedback, runner, spend
from core.office import AGENTS
from core.seo import monitor as seo_monitor, scheduler as seo_scheduler
from core.web import start_web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
dp = Dispatcher()
WAIT_REASON: dict[int, str] = {}  # user_id -> job_id, ждём причину переделки
BOT: Bot | None = None

ROUTES = {"design": "designer", "d": "designer", "web": "webdesigner", "w": "webdesigner",
          "smm": "smm", "s": "smm", "seo": "seo"}
ICON = {"designer": "🎨", "webdesigner": "🖥", "smm": "📣", "seo": "🔎"}

HELP = (
    "Команда dsgners на связи.\n\n"
    "Просто текст — задача графическому дизайнеру. Другим агентам — с командой:\n"
    "/web задача — веб-дизайнер (макеты блоков и страниц сайта)\n"
    "/smm задача — SMM-щик (посты в Telegram; публикует только после твоего «Ок»)\n"
    "/seo задача — SEO-шник (аудит, мета-теги, тексты, ТЗ)\n\n"
    "Картинку можно прислать с подписью-задачей (тоже с командой, если не дизайнеру).\n\n"
    "/seo_status — SEO-мониторинг: когда что проверялось и какие проблемы\n"
    "/seo_scan hourly|new|daily|weekly|monthly — запустить проверку или разбор сейчас\n"
    "/seo_page адрес — быстрая SEO-проверка страницы (без ИИ, бесплатно)\n"
    "/seo_fix адрес — оформить статью для поиска (title, description, разметка, alt, ссылки)\n"
    "/article тема — написать статью: сначала план на согласование, после «Ок» — текст\n"
    "/seo_plan — контент-план сейчас (сам приходит по вторникам): что писать и что обновлять по данным\n"
    "Файл CSV/XLSX из Вордстата — загрузить спрос в базу SEO-шника\n\n"
    "/office — ссылка на ИИ-офис\n"
    "/rules [smm|web|seo] — правила, выученные из твоих правок\n"
    "/spend — расходы на API за месяц\n"
    "/rollback — вернуть предыдущую версию системы"
)


def route(text: str) -> tuple[str, str]:
    """«/smm пост про…» → ("smm", "пост про…"). Без команды — дизайнеру."""
    t = (text or "").strip()
    if t.startswith("/"):
        head, _, rest = t.partition(" ")
        cmd = head[1:].split("@")[0].lower()
        if cmd in ROUTES:
            return ROUTES[cmd], rest.strip()
    return "designer", t


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


@dp.message(Command("office"))
async def office_link(m: Message):
    if not is_owner(m):
        return
    dom = config.ROOT / ".office_domain"
    if dom.exists() and config.OFFICE_TOKEN:
        await m.answer(f"https://{dom.read_text().strip()}/?token={config.OFFICE_TOKEN}\n\nНикому не пересылай — по ссылке полный доступ.")
    else:
        await m.answer("Офис ещё не установлен — запусти deploy/install.sh на сервере.")


@dp.message(Command("rollback"))
async def rollback(m: Message):
    if not is_owner(m):
        return
    d = config.DATA / "deploy"
    if not (d / "history").exists() or not (d / "history").read_text().strip():
        await m.answer("Откатывать некуда — это первая установленная версия.")
        return
    d.mkdir(parents=True, exist_ok=True)
    (d / "rollback_request").write_text("1")
    await m.answer("Ок, верну предыдущую версию в течение минуты (если агент сейчас работает — после его задачи).")


@dp.message(Command("rules"))
async def rules(m: Message):
    if not is_owner(m):
        return
    arg = (m.text or "").partition(" ")[2].strip().lower()
    agent = ROUTES.get(arg, "designer")
    txt = feedback.read(agent).strip() or "Пока пусто — правила появятся после первой «Переделать»."
    await m.answer(txt[-3800:])


@dp.message(Command("spend"))
async def spend_cmd(m: Message):
    if not is_owner(m):
        return
    parts = [f"{AGENTS.get(a, {}).get('title', a)}: ${v:.2f}" for a, v in spend.by_agent_this_month().items()]
    await m.answer(f"Расход за месяц: ${spend.this_month():.2f} из ${config.MONTHLY_LIMIT_USD:.0f}\n" + "\n".join(parts))


SCAN = {"hourly": "hourly", "new": "new_pages", "new_pages": "new_pages", "daily": "daily", "weekly": "weekly",
        "monthly": "monthly", "month": "monthly"}


@dp.message(Command("seo_status"))
async def seo_status(m: Message):
    if is_owner(m):
        await m.answer(seo_monitor.status_text()[:3900])


@dp.message(Command("seo_scan"))
async def seo_scan(m: Message):
    if not is_owner(m):
        return
    arg = (m.text or "").partition(" ")[2].strip().lower() or "hourly"
    name = SCAN.get(arg)
    if not name:
        await m.answer("Варианты: /seo_scan hourly | new | daily | weekly | monthly")
        return
    if name in ("weekly", "monthly"):
        await m.answer("Запустил — это может занять до часа, отчёт пришлю." if name == "weekly"
                       else "Запустил разбор за месяц — отчёт и выводы пришлю.")
        asyncio.create_task(seo_scheduler.run(name))
        return
    await m.answer(f"Проверяю ({arg})…")
    await m.answer(f"🔎 {arg}: {await seo_scheduler.run(name)}")


def _url_arg(m: Message) -> str:
    u = (m.text or "").partition(" ")[2].strip()
    return u if not u or u.startswith("http") else "https://" + u


@dp.message(Command("seo_page"))
async def seo_page(m: Message):
    if not is_owner(m):
        return
    url = _url_arg(m)
    if not url:
        await m.answer("Пример: /seo_page https://dsgners.ru/…")
        return
    from core.seo import page
    rep = await page.check_url(url, is_article=seo_monitor.is_content(url))
    await m.answer(rep.summary()[:3900])


@dp.message(Command("seo_fix"))
async def seo_fix(m: Message):
    if not is_owner(m):
        return
    url = _url_arg(m)
    if not url:
        await m.answer("Пример: /seo_fix https://dsgners.ru/… — SEO-шник подготовит title, description, разметку, alt и ссылки.")
        return
    from core.seo import page
    rep = await page.check_url(url, is_article=True)
    try:
        seo_monitor.start_optimize(url, rep, auto=False)
    except ValueError as e:
        await m.answer(str(e))
        return
    await m.answer("🔎 SEO-шник взял статью. Пришлю готовый пакет: что вставить в поля, разметку и alt.")


async def seo_notify(text: str, files: list):
    if not BOT:
        return
    for uid in config.OWNER_IDS:
        await BOT.send_message(uid, text[:3900])
        for f in files:
            await BOT.send_document(uid, FSInputFile(f))


def seo_start_job(agent: str, task: str, source: str, full_task: str) -> str:
    return runner.start_job(agent, task, source=source, full_task=full_task)


async def seo_after_approve(job: dict):
    """«Ок» на план статьи → SEO-шник пишет текст; «Ок» на правила из разбора → правила вступают в силу."""
    meta = job.get("meta") or {}
    if job["agent"] != "seo":
        return
    if meta.get("stage") == "plan":
        plan_file = next((config.OUTBOX / f for f in job["files"] if f.endswith("/plan.md")), None)
        plan = plan_file.read_text("utf-8") if plan_file and plan_file.exists() else ""
        title = meta.get("title") or job["task"]
        kind = "Обновление" if job["task"].startswith("Обновить") else "Статья"
        runner.start_job("seo", f"{kind}: {title}", source="telegram", parent=job["id"],
                         full_task=f"Статья по утверждённому плану (навык «Написать статью», этап 2).\n\n"
                                   f"Исходная задача: {job['task']}\n\n{plan}")
        for uid in config.OWNER_IDS:
            await BOT.send_message(uid, f"✍️ План принят — SEO-шник пишет статью «{title}». Пришлю текст и SEO-пакет.")
    if meta.get("topics"):
        topics = meta["topics"][: config.SEO_TOPICS_PER_WEEK + 3]
        for t in topics:
            if t.get("kind") == "update" and t.get("url"):
                task, body = f"Обновить: {t['title']}", f"Обновить статью {t['url']}"
            else:
                task, body = f"Статья: {t['title']}", f"Написать статью «{t['title']}»"
            runner.start_job("seo", task, source="content_plan", parent=job["id"], full_task=(
                f"{body}\n\nИспользуй навык «Написать статью» — сейчас этап 1, только план.\n"
                f"Главный запрос: {t.get('main_query', '')}\nПочему эта тема: {t.get('why', '')}\n"
                f"Чем будем лучше топа: {t.get('angle', '')}"))
        seo_monitor.remember_topics(topics)
        for uid in config.OWNER_IDS:
            await BOT.send_message(uid, f"🗂 Контент-план принят: {len(topics)} тем. SEO-шник готовит планы статей"
                                        + (" и сразу пишет тексты." if config.SEO_AUTO_PLANS else " — пришлю по одному на «Ок»."))
    if meta.get("rules"):
        for r in meta["rules"]:
            feedback.add_rule("seo", r, "месячный разбор результатов")
        for uid in config.OWNER_IDS:
            await BOT.send_message(uid, f"📌 Добавил SEO-шнику правил: {len(meta['rules'])}. Посмотреть: /rules seo")


@dp.message(Command("seo_plan"))
async def seo_plan(m: Message):
    if not is_owner(m):
        return
    await m.answer("Собираю данные (Вебмастер, Метрика, Вордстат) — SEO-шник пришлёт контент-план на неделю.")
    await m.answer(f"🔎 {await seo_scheduler.run('content_plan')}")


@dp.message(Command("article"))
async def article(m: Message):
    if not is_owner(m):
        return
    topic = (m.text or "").partition(" ")[2].strip()
    if not topic:
        await m.answer("Пример: /article как собрать портфолио дизайнеру без опыта\n"
                       "Можно добавить ссылки на работы авторов dsgners, которые стоит показать в статье.")
        return
    await _start(m, "seo", f"Статья: {topic}",
                 f"Написать статью: {topic}\n\nИспользуй навык «Написать статью» — сейчас этап 1, только план.")


@dp.message(F.photo | F.document)
async def with_image(m: Message, bot: Bot):
    if not is_owner(m):
        return
    fname = (m.document.file_name or "").lower() if m.document else ""
    if m.document and fname.endswith((".csv", ".xlsx", ".xlsm", ".txt")):
        from core.seo import keywords
        f = await bot.download(m.document.file_id)
        try:
            n = keywords.import_file(fname, f.read(), source=(m.caption or "wordstat")[:40])
        except Exception as e:
            await m.answer(f"Не смог прочитать файл: {e}")
            return
        await m.answer(f"📥 Вордстат: загрузил {n} фраз, всего в базе {keywords.count()}. "
                       "SEO-шник учтёт их в контент-плане и статьях." if n else
                       "Не нашёл в файле пар «фраза — частотность». Пришли выгрузку Вордстата как есть (CSV или XLSX).")
        return
    if m.document and not (m.document.mime_type or "").startswith("image/"):
        await m.answer("Понимаю картинки и выгрузки Вордстата (CSV/XLSX). Другие документы — в офисе → «Знания».")
        return
    file_id = m.photo[-1].file_id if m.photo else m.document.file_id
    ext = ".jpg" if m.photo else ("." + (m.document.file_name or "x.png").rsplit(".", 1)[-1])
    dest = config.DATA / "uploads" / f"{uuid.uuid4().hex[:10]}{ext}"
    await bot.download(file_id, destination=dest)
    agent, shown = route(m.caption or "")
    shown = shown or "Визуал с прикреплённой работой"
    await _start(m, agent, shown, f"{shown}\n\nПрикреплённая картинка: {dest}")


async def _start(m: Message, agent: str, task: str, full: str | None = None):
    if not task:
        await m.answer("Напиши задачу после команды, например: /smm анонс статьи https://dsgners.ru/…")
        return
    try:
        runner.start_job(agent, task, source="telegram", full_task=full)
    except ValueError as e:
        await m.answer(str(e))
        return
    await m.answer(f"{ICON.get(agent, '🤖')} {AGENTS[agent]['title']} взял задачу. Пришлю результат сюда.")


@dp.message(F.text)
async def text_task(m: Message):
    if not is_owner(m):
        return
    uid = m.from_user.id
    if uid in WAIT_REASON:
        jid = WAIT_REASON.pop(uid)
        try:
            runner.redo(jid, m.text, source="telegram")
            await m.answer("Записал в правила. Переделываю…")
        except ValueError as e:
            await m.answer(f"Не вышло: {e}")
        return
    agent, task = route(m.text)
    await _start(m, agent, task)


IMG = (".png", ".jpg", ".jpeg", ".webp")


async def send_review(job: dict):
    """Готовая работа → владельцам в Telegram: картинки альбомом, документы файлами, посты — превью."""
    if not BOT:
        return
    if (config.SEO_AUTO_PLANS and job["agent"] == "seo" and job.get("source") == "content_plan"
            and (job.get("meta") or {}).get("stage") == "plan"):
        runner.approve(job["id"])   # план из утверждённого контент-плана — сразу в работу, без согласования
        return
    files = [config.OUTBOX / f for f in job["files"]]
    imgs = [f for f in files if f.suffix.lower() in IMG and f.exists()][:10]
    docs = [f for f in files if f.suffix.lower() not in IMG and f.exists() and f.name != "posts.md"][:5]
    posts = (job.get("meta") or {}).get("posts") or []
    title = f"{ICON.get(job['agent'], '🤖')} {AGENTS[job['agent']]['title']}"
    ok_text = "✅ Ок и опубликовать" if any(p.get("channel") for p in posts) else "✅ Ок"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=ok_text, callback_data=f"ok:{job['id']}"),
        InlineKeyboardButton(text="🔁 Переделать", callback_data=f"redo:{job['id']}"),
    ]])
    for uid in config.OWNER_IDS:
        if len(imgs) == 1:
            await BOT.send_photo(uid, FSInputFile(imgs[0]))
        elif imgs:
            await BOT.send_media_group(uid, [InputMediaPhoto(media=FSInputFile(f)) for f in imgs])
        for d in docs:
            await BOT.send_document(uid, FSInputFile(d))
        for i, p in enumerate(posts, 1):
            await BOT.send_message(uid, f"📝 Пост {i} → {p.get('channel') or 'черновик'}\n\n{p['text'][:3900]}")
        await BOT.send_message(uid, f"{title}: {job['caption'][:3400]}\n\n💸 ${job['cost']:.3f}", reply_markup=kb)


async def publish_posts(job: dict):
    """После «Ок»: SMM-посты уходят в каналы. Бот должен быть админом канала."""
    posts = (job.get("meta") or {}).get("posts") or []
    if not BOT or not posts:
        return
    from core.office import office
    results = []
    for p in posts:
        ch = (p.get("channel") or "").strip()
        if not ch:
            results.append("черновик — не публиковал")
            continue
        try:
            img = p.get("image")
            if img and len(p["text"]) <= 1024:
                await BOT.send_photo(ch, FSInputFile(img), caption=p["text"])
            else:
                if img:
                    await BOT.send_photo(ch, FSInputFile(img))
                await BOT.send_message(ch, p["text"])
            results.append(f"{ch}: опубликовано")
            office.emit(job["agent"], "done", f"Опубликовано в {ch}", job["id"])
        except Exception as e:
            results.append(f"{ch}: ошибка — {e}")
            office.emit(job["agent"], "error", f"Не опубликовал в {ch}: {e}", job["id"])
    meta = dict(job.get("meta") or {}, published=results)
    office.update_job(job["id"], meta=meta)
    for uid in config.OWNER_IDS:
        await BOT.send_message(uid, "Публикация:\n" + "\n".join(results))


@dp.callback_query(F.data.startswith("ok:"))
async def approve(c: CallbackQuery):
    if not is_owner(c):
        return
    try:
        job = runner.approve(c.data.split(":", 1)[1])
    except ValueError as e:
        await c.answer(str(e))
        return
    await c.message.edit_reply_markup(reply_markup=None)
    if job["status"] != "approved":
        await c.answer("Эта задача уже не на согласовании")
        return
    for f in job["files"]:
        if f.lower().endswith(IMG):
            await c.message.answer_document(FSInputFile(config.OUTBOX / f), caption="Исходник без сжатия")
    await c.answer("Принято 👍")


@dp.callback_query(F.data.startswith("redo:"))
async def redo(c: CallbackQuery):
    if not is_owner(c):
        return
    WAIT_REASON[c.from_user.id] = c.data.split(":", 1)[1]
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("Что не так? Напиши одним сообщением — это станет правилом, и агент переделает.")
    await c.answer()


async def main():
    global BOT
    await start_web()
    logging.info("ИИ-офис слушает порт %s", config.OFFICE_PORT)
    seo_monitor.setup(seo_notify, seo_start_job)
    asyncio.create_task(seo_scheduler.loop())
    if not config.TELEGRAM_BOT_TOKEN:
        logging.warning("Нет TELEGRAM_BOT_TOKEN — работает только офис")
        await asyncio.Event().wait()
    BOT = Bot(config.TELEGRAM_BOT_TOKEN)
    runner.on_review(send_review)
    runner.on_approved(publish_posts)
    runner.on_approved(seo_after_approve)
    await dp.start_polling(BOT)


if __name__ == "__main__":
    asyncio.run(main())
