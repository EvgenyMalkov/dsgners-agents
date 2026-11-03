"""Главный процесс: Telegram-бот + веб-сервер ИИ-офиса.

Задачи из Telegram и из офиса идут через core.runner, история общая.
Готовая работа приходит владельцам в Telegram с кнопками «Ок / Переделать»
(их же можно нажать в офисе — что раньше, то и считается).
"""
import asyncio
import time
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
WAIT_REASON: dict[int, str] = {}
MSG_JOB: dict[int, str] = {}      # message_id сообщений с результатом -> job_id (ответ на них = правка)  # user_id -> job_id, ждём причину переделки
WAIT_BULK: set[int] = set()       # ждём комментарий к пробной партии массового SEO
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
    "/seo_tips on|off — письма авторам с советами в день публикации\n"
    "/seo_stop — остановить SEO-агента (снять очередь, выключить автопилот); /seo_auto on|off — автопилот\n"
    "/seo_bulk — массово оформить старые статьи (title, description, OG, alt): пробная партия → «Ок» → все\n"
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


async def seo_publish(job: dict):
    """«Ок» на статью редакции → картинки, текст и SEO-поля уходят на сайт."""
    from core import publish, site_api
    from core.office import office
    from core.seo import store as seo_store
    if not site_api.enabled():
        for uid in config.OWNER_IDS:
            await BOT.send_message(uid, "ℹ️ API сайта ещё не подключено — статью нужно опубликовать вручную "
                                        "(текст, картинки и SEO-пакет — в файлах выше).")
        return
    probs = publish.problems(job)
    if probs:
        for uid in config.OWNER_IDS:
            await BOT.send_message(uid, "⏸ Не публикую: " + "; ".join(probs)
                                   + "\nЗамени пометки сам и опубликуй вручную, или «Переделать» с подсказкой (например, ссылками на работы).")
        return
    try:
        url, log = await publish.publish(job, seo_monitor.is_own)
        seo_store.mark_optimized(url)       # мониторинг не будет оформлять её второй раз
        office.update_job(job["id"], meta=dict(job.get("meta") or {}, published_url=url, applied=log))
        office.emit("seo", "done", f"Опубликовано: {url}", job["id"])
        text = f"🚀 {url}\n" + "\n".join(log)
    except Exception as e:
        office.emit("seo", "error", f"Публикация не удалась: {e}", job["id"])
        text = f"⚠️ Публикация не удалась: {e}\nФайлы статьи — в сообщении выше, можно опубликовать вручную."
    for uid in config.OWNER_IDS:
        await BOT.send_message(uid, text)


async def seo_apply_site(job: dict):
    """После «Ок»: SEO-поля → на сайт, письмо → автору (почта + уведомление). Нужен API сайта (docs/site-api.md)."""
    from core import site_api
    from core.office import office
    meta = job.get("meta") or {}
    fields, letter = meta.get("seo_fields"), meta.get("letter")
    if meta.get("article"):
        await seo_publish(job)
        return
    if not (fields or letter):
        return
    if not site_api.enabled():
        for uid in config.OWNER_IDS:
            await BOT.send_message(uid, "ℹ️ API сайта ещё не подключено — SEO-поля и письмо автору нужно внести вручную "
                                        "(они в файлах выше). Как подключить: docs/site-api.md.")
        return
    from core.seo import store as seo_store
    res = []
    url = (fields or {}).get("url") or (letter or {}).get("url") or ""
    if not url.startswith("http") or "{URL}" in url:
        for uid in config.OWNER_IDS:
            await BOT.send_message(uid, "ℹ️ Статья ещё не на сайте — SEO-поля не записывал (они в пакете выше).")
        return
    if fields and fields.get("jsonld"):
        fields = dict(fields, jsonld=fields["jsonld"].replace("{URL}", url))
    before = {}
    try:
        art = await site_api.get_article(url)
        aid = art["id"]
        if fields:
            before = art.get("seo") or {}
            if art.get("images"):
                before = dict(before, images=art["images"])
            seo_store.put(f"seo_before:{aid}", before)   # прежние значения — чтобы можно было вернуть
            await site_api.update_seo(aid, fields)
            res.append("✅ SEO-поля записаны на сайт")
        if letter:
            last = seo_store.get(f"letter:{aid}", 0)
            if seo_monitor.is_own(url) or str(art.get("author", "")).lower() in config.SEO_OWN_AUTHORS:
                res.append("письмо не отправлял: это статья редакции")
            elif time.time() - last < 30 * 86400:
                res.append("письмо не отправлял: этому автору по этой статье уже писали меньше 30 дней назад")
            else:
                await site_api.message_author(aid, letter["subject"], letter["text"])
                seo_store.put(f"letter:{aid}", time.time())
                res.append(f"✉️ Письмо автору {art.get('author', '')} отправлено (почта + уведомление)")
    except Exception as e:
        res.append(f"⚠️ Не получилось: {e}")
    office.update_job(job["id"], meta=dict(meta, applied=res))
    office.emit("seo", "done" if not any(r.startswith("⚠️") for r in res) else "error", "; ".join(res)[:200], job["id"])
    for uid in config.OWNER_IDS:
        await BOT.send_message(uid, f"{url}\n" + "\n".join(res))


async def seo_after_approve(job: dict):
    """«Ок» на план статьи → SEO-шник пишет текст; «Ок» на правила из разбора → правила вступают в силу."""
    meta = job.get("meta") or {}
    if job["agent"] != "seo":
        return
    await seo_apply_site(job)
    if meta.get("stage") == "plan" and job["task"].startswith("Автору"):
        meta = dict(meta, stage=None)   # статью автора не переписываем никогда — только SEO-поля и письмо
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
            if t.get("kind") in ("update", "author") and t.get("url") and not seo_monitor.is_own(t["url"]):
                t["kind"] = "author"
            if t.get("kind") == "author" and t.get("url"):
                task = f"Автору: {t['title']}"
                body = (f"Статья автора {seo_monitor.author_of(t['url'])}: {t['url']}\n\n"
                        "Используй навык «Рекомендации автору»: SEO-поля + письмо автору, текст не переписывай.")
            elif t.get("kind") == "update" and t.get("url"):
                task = f"Обновить: {t['title']}"
                body = (f"Обновить статью редакции {t['url']}\n\n"
                        "Используй навык «Написать статью» — сейчас этап 1, только план.")
            else:
                task = f"Статья: {t['title']}"
                body = (f"Написать статью «{t['title']}» (публикуется в профиле редакции)\n\n"
                        "Используй навык «Написать статью» — сейчас этап 1, только план.")
            runner.start_job("seo", task, source="content_plan", parent=job["id"], full_task=(
                f"{body}\n"
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


BULK_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="✅ Применить ко всем", callback_data="bulk_ok"),
    InlineKeyboardButton(text="🔁 Переделать", callback_data="bulk_redo")]])


async def _bulk_sample(m: Message):
    from core.seo import bulk
    await m.answer("Собираю статьи с проблемами из последнего обхода и делаю пробную партию — пара минут.")
    text, path = await bulk.run_sample(seo_monitor.is_content)
    if path:
        await m.answer_document(FSInputFile(path), caption="Было → стало, пробная партия")
        await m.answer(text, reply_markup=BULK_KB)
    else:
        await m.answer(text)


@dp.message(Command("seo_bulk"))
async def seo_bulk(m: Message):
    if not is_owner(m):
        return
    from core.seo import bulk
    if (m.text or "").partition(" ")[2].strip() == "status":
        await m.answer(bulk.status_text())
        return
    if bulk.state().get("phase") == "running":
        await m.answer("Уже идёт.\n" + bulk.status_text())
        return
    await _bulk_sample(m)


@dp.callback_query(F.data == "bulk_ok")
async def bulk_ok(c: CallbackQuery):
    if not is_owner(c):
        return
    from core import site_api
    from core.seo import bulk
    bulk.approve()
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("🚀 Запустил. Пробная партия и дальше — порциями по "
                           f"{config.SEO_BULK_PER_HOUR} в час" + (" сразу на сайт." if site_api.enabled() else
                           "; API сайта нет, поэтому в конце пришлю один файл для импорта разработчику.")
                           + " Ход — /seo_bulk status, отчёт раз в день.")
    asyncio.create_task(seo_scheduler.run("bulk"))
    await c.answer()


@dp.callback_query(F.data == "bulk_redo")
async def bulk_redo(c: CallbackQuery):
    if not is_owner(c):
        return
    WAIT_BULK.add(c.from_user.id)
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("Что поправить? Напиши одним сообщением — это станет правилом для всех статей, и я переделаю пробную партию.")
    await c.answer()


@dp.message(Command("seo_auto"))
async def seo_auto(m: Message):
    if not is_owner(m):
        return
    from core.seo import store as seo_store
    arg = (m.text or "").partition(" ")[2].strip().lower()
    if arg in ("on", "вкл", "1"):
        seo_store.put("autopilot", True)
    elif arg in ("off", "выкл", "0"):
        seo_store.put("autopilot", False)
    on = seo_monitor.autopilot()
    await m.answer(("🟢 Автопилот SEO включён: агент сам оформляет новые статьи, разбирает недельный отчёт, "
                    "делает контент-план и разбор за месяц." if on else
                    "⏸ Автопилот SEO выключен: мониторинг и алерты работают (без ИИ), задачи агенту — только по командам "
                    "/seo_fix, /article, /seo_plan, /seo.") + "\nПереключить: /seo_auto on | off")


@dp.message(Command("seo_tips"))
async def seo_tips(m: Message):
    if not is_owner(m):
        return
    from core.seo import author_tips, store as seo_store
    arg = (m.text or "").partition(" ")[2].strip().lower()
    if arg in ("on", "вкл", "1"):
        seo_store.put("author_tips", True)
    elif arg in ("off", "выкл", "0"):
        seo_store.put("author_tips", False)
    on = author_tips.enabled()
    await m.answer(("✉️ Советы авторам включены: каждая новая статья автора проверяется в течение ~30 минут, "
                    "письмо с советами приходит тебе на «Ок»." if on else "Советы авторам выключены.")
                   + "\nПереключить: /seo_tips on | off")


@dp.message(Command("seo_stop"))
async def seo_stop(m: Message):
    """Выключить автопилот и снять с SEO-агента все задачи в очереди."""
    if not is_owner(m):
        return
    from core.office import office
    from core.seo import store as seo_store
    seo_store.put("autopilot", False)
    n = runner.cancel_queued("seo")
    await m.answer(f"⏸ SEO-агент остановлен: автопилот выключен, из очереди снято задач: {n}. "
                   "Задача, которую он делает прямо сейчас, доработает до конца. Включить снова: /seo_auto on")


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
    if uid in WAIT_BULK:
        from core.seo import bulk
        WAIT_BULK.discard(uid)
        bulk.add_rule(m.text)
        await m.answer("Записал правило.")
        await _bulk_sample(m)
        return
    if uid in WAIT_REASON:
        jid = WAIT_REASON.pop(uid)
        try:
            runner.redo(jid, m.text, source="telegram")
            await m.answer("Записал в правила. Переделываю…")
        except ValueError as e:
            await m.answer(f"Не вышло: {e}")
        return
    rep = m.reply_to_message
    if rep and rep.message_id in MSG_JOB and not m.text.startswith("/"):
        try:
            runner.redo(MSG_JOB[rep.message_id], m.text, source="telegram")
            await m.answer("Понял, это правка к той работе. Записал в правила и переделываю…")
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
    from core import site_api
    meta = job.get("meta") or {}
    ok_text = "✅ Ок и опубликовать" if any(p.get("channel") for p in posts) else "✅ Ок"
    if job["agent"] == "seo" and site_api.enabled() and meta.get("article"):
        ok_text = "✅ Ок и опубликовать на сайт"
    elif job["agent"] == "seo" and site_api.enabled() and (meta.get("seo_fields") or meta.get("letter")):
        ok_text = "✅ Ок и отправить на сайт" + (" и автору" if meta.get("letter") else "")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=ok_text, callback_data=f"ok:{job['id']}"),
        InlineKeyboardButton(text="🔁 Переделать", callback_data=f"redo:{job['id']}"),
    ]])
    for uid in config.OWNER_IDS:
        if len(imgs) == 1:
            sent = await BOT.send_photo(uid, FSInputFile(imgs[0]))
            MSG_JOB[sent.message_id] = job["id"]
        elif imgs:
            for sent in await BOT.send_media_group(uid, [InputMediaPhoto(media=FSInputFile(f)) for f in imgs]):
                MSG_JOB[sent.message_id] = job["id"]
        for d in docs:
            await BOT.send_document(uid, FSInputFile(d))
        if meta.get("letter"):
            L = meta["letter"]
            await BOT.send_message(uid, f"✉️ Письмо автору (уйдёт после «Ок»)\nТема: {L['subject']}\n\n{L['text'][:3500]}")
        if meta.get("seo_fields"):
            f = meta["seo_fields"]
            await BOT.send_message(uid, "🔧 SEO-поля для сайта:\n" + "\n".join(
                f"{k}: {f[k]}" for k in ("seo_title", "meta_description", "og_title") if f.get(k))
                + (f"\nalt картинок: {len(f.get('images') or [])}; разметка: {'да' if f.get('jsonld') else 'нет'}"))
        for i, p in enumerate(posts, 1):
            await BOT.send_message(uid, f"📝 Пост {i} → {p.get('channel') or 'черновик'}\n\n{p['text'][:3900]}")
        sent = await BOT.send_message(uid, f"{title}: {job['caption'][:3400]}\n\n💸 ${job['cost']:.3f}\n\n"
                                      "Правки — ответом на это сообщение или кнопкой «Переделать».", reply_markup=kb)
        MSG_JOB[sent.message_id] = job["id"]


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
