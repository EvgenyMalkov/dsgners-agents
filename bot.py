"""Главный процесс: Telegram-бот + веб-сервер ИИ-офиса.

Задачи из Telegram и из офиса идут через core.runner, история общая.
Готовая работа приходит владельцам в Telegram с кнопками «Ок / Переделать»
(их же можно нажать в офисе — что раньше, то и считается).
"""
import asyncio
import time
import logging
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, KeyboardButton, MenuButtonWebApp, Message,
                           LinkPreviewOptions, ReplyKeyboardMarkup, WebAppInfo)

from core import config, feedback, runner, spend
from core.office import AGENTS, office
from core.seo import monitor as seo_monitor, scheduler as seo_scheduler
from core.web import start_web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
dp = Dispatcher()
# PR-агент: команды /pr_* регистрируем первыми — раньше общего обработчика текста
from core.pr import jobs as pr_jobs, tg as pr_tg  # noqa: E402
from core.pr.tg import (pr_author, pr_digest, pr_login, pr_outreach, pr_pause, pr_report, pr_resume,  # noqa: E402,F401
                        pr_scan, pr_src, pr_status, pr_stop, pr_test, pr_mailtest)
pr_tg.register(dp)
WAIT_REASON: dict[int, str] = {}
MSG_JOB: dict[int, str] = {}      # message_id сообщений с результатом -> job_id (ответ на них = правка)  # user_id -> job_id, ждём причину переделки
WAIT_BULK: set[int] = set()       # ждём комментарий к пробной партии массового SEO
BOT: Bot | None = None
NEXT_AGENT: dict[int, str] = {}   # выбрал агента в меню — следующее сообщение уйдёт ему
WAIT_INPUT: dict[int, str] = {}
WAIT_OWN: dict[int, tuple[str, str]] = {}   # ждём свой текст поста: user_id -> (flow_id, стиль)   # ждём ввод для пункта меню (адрес, тема…) -> команда

ROUTES = {"design": "designer", "d": "designer", "designer": "designer", "web": "webdesigner", "w": "webdesigner",
          "webdesigner": "webdesigner", "smm": "smm", "s": "smm", "seo": "seo"}
ICON = {"designer": "🎨", "webdesigner": "🖥", "smm": "📣", "seo": "🔎"}

HELP = (
    "Команда dsgners на связи.\n\n"
    "Просто напиши задачу — её возьмёт графический дизайнер. Другому агенту — выбери его в меню ниже "
    "(или /web, /smm, /seo перед текстом). Картинку можно прислать с подписью-задачей.\n\n"
    "Правка к готовой работе — ответом на её сообщение. Всё остальное — в меню 👇"
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
    await m.answer("👋", reply_markup=reply_kb())
    await m.answer(HELP, reply_markup=menu_kb("main"))


@dp.message(Command("help", "menu"))
async def help_(m: Message):
    if is_owner(m):
        await m.answer(HELP, reply_markup=menu_kb("main"))


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


SCAN = {"hourly": "hourly", "new": "new_pages", "new_pages": "new_pages", "daily": "daily", "weekly": "weekly", "geo": "geo",
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
        await m.answer("Варианты: /seo_scan hourly | new | daily | weekly | geo | monthly")
        return
    if name in ("weekly", "monthly", "geo"):
        await m.answer("Запустил — это может занять до часа, отчёт пришлю." if name in ("weekly", "geo")
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
            if art.get("is_editorial") or seo_monitor.is_own(url) or str(art.get("author", "")).lower() in config.SEO_OWN_AUTHORS:
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


@dp.message(Command("ws"))
async def ws_cmd(m: Message):
    """/ws фраза — частота, что ищут, динамика, регионы; /ws фраза1 | фраза2 — сравнение."""
    if not is_owner(m):
        return
    from core.seo import wordstat
    arg = (m.text or "").partition(" ")[2].strip()
    if not wordstat.enabled():
        await m.answer("Вордстат API не подключён: впиши WORDSTAT_API_KEY и YC_FOLDER_ID в .env на сервере. "
                       "Пока можно прислать выгрузку CSV/XLSX файлом.")
        return
    if not arg:
        await m.answer("Примеры:\n/ws портфолио дизайнера — частота, что ищут, динамика, регионы\n"
                       "/ws портфолио дизайнера | портфолио ux | как сделать портфолио — сравнить\n"
                       "/ws_parse портфолио дизайнера — собрать семантику в базу SEO-шника")
        return
    try:
        if "|" in arg:
            phrases = [p.strip() for p in arg.split("|") if p.strip()]
            await m.answer(f"Сравниваю {len(phrases)} фраз…")
            await m.answer("📈 Вордстат, сравнение\n\n" + wordstat.fmt_compare(await wordstat.compare(phrases)))
            return
        top = await wordstat.top(arg, 200)
        dyn = await wordstat.dynamics(arg)
        reg = await wordstat.regions(arg, "regions", limit=7)
        await m.answer(f"📈 {wordstat.fmt_top(top, 15)}\n\nПо месяцам:\n{wordstat.fmt_dynamics(dyn)}"
                       f"\n\nГде ищут чаще:\n{wordstat.fmt_regions(reg)}"[:3900])
    except Exception as e:
        await m.answer(f"Вордстат: {e}"[:1000])


@dp.message(Command("ws_parse"))
async def ws_parse(m: Message):
    if not is_owner(m):
        return
    from core.seo import keywords, wordstat
    arg = (m.text or "").partition(" ")[2].strip()
    if not arg or not wordstat.enabled():
        await m.answer("Пример: /ws_parse портфолио дизайнера" if wordstat.enabled() else
                       "Вордстат API не подключён: WORDSTAT_API_KEY и YC_FOLDER_ID в .env.")
        return
    await m.answer(f"Собираю семантику по «{arg}» (топ + похожие, на шаг глубже)…")
    try:
        r = await wordstat.parse(arg, 1000, depth=2)
    except Exception as e:
        await m.answer(f"Вордстат: {e}"[:1000])
        return
    await m.answer(f"📥 «{arg}»: собрал {r['collected']} фраз, всего в базе {keywords.count()}. "
                   "SEO-шник учтёт их в контент-плане и статьях.\n\n"
                   + "\n".join(f"{x['count']:>8}  {x['phrase']}" for x in r["top"][:20]))


@dp.message(Command("geo_check"))
async def geo_check(m: Message):
    """/geo_check — прогнать весь набор по нейроответам; /geo_check запрос — один запрос."""
    if not is_owner(m):
        return
    from core.seo import geo
    if not geo.enabled():
        await m.answer("Нейроответы (GEO) не подключены: " + geo.why_off())
        return
    arg = (m.text or "").partition(" ")[2].strip()
    if arg:
        await m.answer(f"Спрашиваю нейросети: «{arg}»…")
        for e in geo.engines():
            r = await geo.check(arg, e, use_cache=False)
            await m.answer(geo.fmt_one(r)[:3900], disable_web_page_preview=True)
        return
    if not geo.queries():
        await m.answer("Набора запросов ещё нет — собираю стартовый: " + geo.seed())
    n = len(geo.queries()) * len(geo.engines())
    await m.answer(f"Прогоняю {len(geo.queries())} запросов × {len(geo.engines())} движк. — это ~{max(1, n * 3 // 60)} мин. "
                   "Пришлю блок «🤖 Нейроответы».")

    async def job():
        await m.answer(f"🔎 GEO: {await seo_scheduler.run('geo')}")
    asyncio.create_task(job())


@dp.message(Command("geo_q"))
async def geo_q(m: Message):
    """/geo_q list | add запрос | del запрос | seed — набор запросов для мониторинга нейроответов."""
    if not is_owner(m):
        return
    from core.seo import geo
    cmd, _, arg = (m.text or "").partition(" ")[2].strip().partition(" ")
    cmd, arg = cmd.lower(), arg.strip()
    if cmd == "add" and arg:
        out = [geo.add_query(q) for q in arg.split("|") if q.strip()]
        await m.answer("\n".join(out))
    elif cmd == "del" and arg:
        await m.answer("Удалил." if geo.del_query(arg) else "Такого запроса в наборе нет.")
    elif cmd == "seed":
        await m.answer(geo.seed(force=True) + " (ручные запросы не трогал)")
    else:
        qs = geo.queries()
        if not qs:
            await m.answer("Набор пуст. /geo_q seed — собрать стартовый (бренд + Вебмастер + Вордстат), "
                           "/geo_q add запрос — добавить свой.")
            return
        L = [f"🤖 Набор запросов для нейроответов: {len(qs)} (максимум {config.GEO_QUERIES_MAX})"]
        for t, name in geo.TYPES.items():
            part = [q for q in qs if q["type"] == t]
            if part:
                L.append(f"\n{name}:")
                L += [f"∙ {q['query']}" + (" ✍️" if q["source"] == "manual" else "") for q in part]
        L.append("\n✍️ — добавлены вручную. /geo_q add запрос (несколько через « | »), /geo_q del запрос, "
                 "/geo_q seed — пересобрать автоматическую часть")
        await m.answer("\n".join(L)[:3900])


@dp.message(Command("geo_report"))
async def geo_report(m: Message):
    if not is_owner(m):
        return
    from core.seo import geo
    text, _ = await geo.report()
    await m.answer((text + "\n\n" + geo.history_text(8))[:3900], disable_web_page_preview=True)


@dp.message(Command("geo_fix"))
async def geo_fix(m: Message):
    """/geo_fix N — оформить статью-кандидата N из блока «в топ-10, но не в нейроответе»."""
    if not is_owner(m):
        return
    from core.seo import page, store as seo_store
    cand = seo_store.get("geo_candidates") or []
    arg = (m.text or "").partition(" ")[2].strip()
    if not cand:
        await m.answer("Кандидатов нет — сначала /geo_check.")
        return
    if not arg.isdigit() or not 1 <= int(arg) <= len(cand):
        await m.answer("Кандидаты:\n" + "\n".join(f"{i}. «{c['query']}» — поз. {c['pos']}, {c['url']}"
                                                  for i, c in enumerate(cand, 1)) + "\n\nПример: /geo_fix 1")
        return
    c = cand[int(arg) - 1]
    rep = await page.check_url(c["url"], is_article=True)
    try:
        seo_monitor.start_optimize(c["url"], rep, auto=False, geo_query=c["query"])
    except ValueError as e:
        await m.answer(str(e))
        return
    await m.answer(f"🔎 SEO-шник взял статью под нейроответ «{c['query']}». Пришлю пакет"
                   + ("." if c.get("own") else " и письмо автору."))


@dp.message(Command("ws_map"))
async def ws_map(m: Message):
    """Карта спроса по разделам SEO-стратегии: курсы, студии по городам, глоссарий, инструменты, хабы, вакансии."""
    if not is_owner(m):
        return
    from core.seo import demand_map, wordstat
    if not wordstat.enabled():
        await m.answer("Вордстат API не подключён: WORDSTAT_API_KEY и YC_FOLDER_ID в .env.")
        return
    n = sum(len(v) for v in demand_map.SECTIONS.values())
    await m.answer(f"Собираю карту спроса: {len(demand_map.SECTIONS)} разделов, {n} фраз. Это 3–7 минут — пришлю файлы.")

    async def go():
        try:
            md, csvp = await demand_map.build()
            await m.answer_document(FSInputFile(md), caption="🗺 Карта спроса (markdown) — можно переслать Claude")
            await m.answer_document(FSInputFile(csvp), caption="То же таблицей (CSV для Excel/Google Таблиц)")
        except Exception as e:
            await m.answer(f"Карта спроса не собралась: {e}"[:1000])
    asyncio.create_task(go())


@dp.message(Command("ws_hubs"))
async def ws_hubs(m: Message):
    """Темы сайта как хабы: спрос из Вордстата + наши запросы из Вебмастера + трафик из Метрики."""
    if not is_owner(m):
        return
    from core.seo import demand_map, wordstat
    if not wordstat.enabled():
        await m.answer("Вордстат API не подключён: WORDSTAT_API_KEY и YC_FOLDER_ID в .env.")
        return
    await m.answer(f"Собираю данные по {len(demand_map.HUBS)} темам сайта (Вордстат + Вебмастер + Метрика). 3–5 минут.")

    async def go():
        try:
            md = await demand_map.build_hubs()
            await m.answer_document(FSInputFile(md), caption="🧭 Хабы: спрос и что у нас уже есть — перешли Claude")
        except Exception as e:
            await m.answer(f"Не собралось: {e}"[:1000])
    asyncio.create_task(go())


@dp.message(Command("topics_traffic"))
async def topics_traffic(m: Message):
    """Посещаемость страниц тем (/ui, /branding…), фильтра и «Образования» по Метрике."""
    if not is_owner(m):
        return
    from core.seo import demand_map
    await m.answer("Смотрю Метрику по темам…")
    try:
        await m.answer((await demand_map.topics_traffic())[:3900])
    except Exception as e:
        await m.answer(f"Метрика: {e}"[:1000])


@dp.message(Command("hub_inventory"))
async def hub_inventory(m: Message):
    """Сколько постов уже есть по каждой подтеме направлений (под хабы) + спрос из Вордстата."""
    if not is_owner(m):
        return
    from core.seo import inventory
    await m.answer("Раскладываю базу постов по направлениям и подтемам… 1–3 минуты.")

    async def go():
        try:
            md = await inventory.build()
            await m.answer_document(FSInputFile(md), caption="🗂 Что уже есть в базе под хабы — перешли Claude")
        except Exception as e:
            await m.answer(f"Не собралось: {e}"[:1000])
    asyncio.create_task(go())


@dp.message(Command("hub_classify"))
async def hub_classify(m: Message):
    """Разложить всю базу постов по направлениям хабов моделью (один проход, продолжается с места остановки)."""
    if not is_owner(m):
        return
    from core.seo import classify
    if "status" in (m.text or ""):
        await m.answer(classify.status())
        return
    await m.answer("Запустил раскладку базы: сначала читаю заголовки страниц (≈1–2 часа, со скоростью обхода), "
                   "потом модель раскладывает посты по направлениям. Ход — /hub_classify status. "
                   "В конце пришлю обновлённую инвентаризацию.")

    async def say(t):
        await m.answer(t)

    async def go():
        from core.seo import inventory
        try:
            await m.answer(await classify.run(say))
            md = await inventory.build()
            await m.answer_document(FSInputFile(md), caption="🗂 База под хабы — с раскладкой моделью. Перешли Claude")
        except Exception as e:
            await m.answer(f"Раскладка остановилась: {e}"[:1000])
    asyncio.create_task(go())


CLUSTER_ALIASES = {"branding": "Брендинг", "брендинг": "Брендинг", "ui": "UI", "ux": "UX", "typography": "Типографика",
                   "типографика": "Типографика", "graphic": "Графический дизайн", "career": "Карьера", "карьера": "Карьера",
                   "ai": "Нейросети", "нейросети": "Нейросети", "motion": "Моушн", "3d": "3D", "product": "Продуктовый дизайн"}


@dp.message(Command("cluster"))
async def cluster_cmd(m: Message):
    """Данные для каркаса кластера: подтемы × намерения запросов × наши статьи × Метрика/Вебмастер."""
    if not is_owner(m):
        return
    from core.seo import cluster
    arg = (m.text or "").partition(" ")[2].strip().lower() or "branding"
    direction = CLUSTER_ALIASES.get(arg, arg.capitalize())
    await m.answer(f"Собираю кластер «{direction}»: Вордстат по каждой подтеме, наши статьи, Метрика и Вебмастер. 2–5 минут.")

    async def go():
        try:
            md = await cluster.build(direction)
            await m.answer_document(FSInputFile(md), caption=f"🧩 Кластер «{direction}» — перешли Claude")
        except Exception as e:
            await m.answer(f"Не собралось: {e}"[:1000])
    asyncio.create_task(go())


@dp.message(Command("cluster_pick"))
async def cluster_pick(m: Message):
    """Подборки статей под блоки подстраницы хаба: /cluster_pick logo (rebrending, firmennyj-stil, aydentika, brendbuk, upakovka, branding)."""
    if not is_owner(m):
        return
    from core.seo import pick
    key = (m.text or "").partition(" ")[2].strip().lower() or "logo"
    if key not in pick.SUBPAGES:
        await m.answer(f"Подстраницы: {pick.keys()}. Пример: /cluster_pick logo")
        return
    await m.answer(f"Раскладываю статьи подтемы по блокам страницы «{key}»… 1–3 минуты.")

    async def go():
        try:
            md = await pick.build(key)
            await m.answer_document(FSInputFile(md), caption=f"📚 Подборки для «{key}» — на утверждение, перешли Claude")
        except Exception as e:
            await m.answer(f"Не собралось: {e}"[:1000])
    asyncio.create_task(go())


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
    if m.document and fname.endswith((".html", ".htm")):
        # HTML-прототип фичи → по умолчанию дизайнеру на ролик
        dest = config.DATA / "uploads" / f"{uuid.uuid4().hex[:10]}-{Path(fname).name}"
        await bot.download(m.document.file_id, destination=dest)
        agent, shown = route(m.caption or "")
        if not (m.caption or "").startswith("/") and m.from_user.id in NEXT_AGENT:
            agent = NEXT_AGENT.pop(m.from_user.id)
        shown = shown or "Ролик по прототипу"
        await _start(m, agent, shown, f"{shown}\n\nПрикреплённый HTML-прототип: {dest}")
        return
    if m.document and not (m.document.mime_type or "").startswith("image/"):
        await m.answer("Понимаю картинки, HTML-прототипы (для роликов) и выгрузки Вордстата (CSV/XLSX). Другие документы — в офисе → «Знания».")
        return
    file_id = m.photo[-1].file_id if m.photo else m.document.file_id
    ext = ".jpg" if m.photo else ("." + (m.document.file_name or "x.png").rsplit(".", 1)[-1])
    dest = config.DATA / "uploads" / f"{uuid.uuid4().hex[:10]}{ext}"
    await bot.download(file_id, destination=dest)
    agent, shown = route(m.caption or "")
    if not (m.caption or "").startswith("/") and m.from_user.id in NEXT_AGENT:
        agent = NEXT_AGENT.pop(m.from_user.id)
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


# ---------- меню на кнопках ----------
def office_url() -> str | None:
    dom = config.ROOT / ".office_domain"
    if dom.exists() and config.OFFICE_TOKEN:
        return f"https://{dom.read_text().strip()}/?token={config.OFFICE_TOKEN}"
    return None


def reply_kb() -> ReplyKeyboardMarkup:
    """Постоянная кнопка под полем ввода."""
    row = [KeyboardButton(text="📋 Меню")]
    if office_url():
        row.append(KeyboardButton(text="🏢 ИИ-офис", web_app=WebAppInfo(url=office_url())))
    return ReplyKeyboardMarkup(keyboard=[row], resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Задача дизайнеру или выбери агента в меню")


def _b(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _onoff(v: bool) -> str:
    return "вкл ✅" if v else "выкл"


def menu_kb(name: str) -> InlineKeyboardMarkup:
    back = [_b("« Назад", "m:main")]
    if name == "main":
        rows = [[_b("🎨 Графический дизайнер", "a:designer")],
                [_b("🖥 Веб-дизайнер", "a:webdesigner"), _b("📣 SMM", "a:smm")],
                [_b("🔎 SEO ▸", "m:seo"), _b("🤝 PR ▸", "m:pr")],
                [_b("⚙️ Система ▸", "m:sys")]]
        if office_url():
            rows.insert(3, [InlineKeyboardButton(text="🏢 Открыть ИИ-офис", web_app=WebAppInfo(url=office_url()))])
    elif name == "seo":
        from core.seo import author_tips
        rows = [[_b("✏️ Задача SEO-шнику", "a:seo")],
                [_b("✍️ Написать статью", "i:article"), _b("🛠 Оформить статью", "i:seo_fix")],
                [_b("📄 Проверить страницу", "i:seo_page"), _b("🗓 Контент-план", "c:/seo_plan")],
                [_b("📊 Статус мониторинга", "c:/seo_status"), _b("🔍 Проверить сейчас ▸", "m:scan")],
                [_b("📦 Массово старые статьи", "c:/seo_bulk"), _b("📦 Ход массовой", "c:/seo_bulk status")],
                [_b(f"🤖 Автопилот: {_onoff(seo_monitor.autopilot())}", "t:auto"),
                 _b(f"✉️ Советы авторам: {_onoff(author_tips.enabled())}", "t:tips")],
                [_b("⏸ Остановить SEO-агента", "c:/seo_stop")],
                [_b("📈 Вордстат: фраза", "i:ws"), _b("🧲 Собрать семантику", "i:ws_parse")],
                [_b("🤖 Нейроответы", "c:/geo_report"), _b("🤖 Проверить запрос", "i:geo_check"),
                 _b("Набор запросов", "c:/geo_q list")],
                [_b("🗺 Карта спроса по разделам", "c:/ws_map"), _b("🧭 Хабы по темам сайта", "c:/ws_hubs")],
                [_b("📊 Посещаемость тем", "c:/topics_traffic"), _b("🗂 База под хабы", "c:/hub_inventory")],
                [_b("🤖 Разложить базу моделью", "c:/hub_classify"), _b("Ход раскладки", "c:/hub_classify status")],
                [_b("🧩 Кластер «Брендинг»", "c:/cluster branding"), _b("📚 Подборки «Логотип»", "c:/cluster_pick logo")],
                [_b("ℹ️ Загрузить выгрузку Вордстата", "h:wordstat")], back]
    elif name == "pr":
        rows = [[_b("📰 Дайджест сейчас", "c:/pr_digest"), _b("🔄 Собрать сейчас", "c:/pr_scan")],
                [_b("🔗 Разобрать статью", "i:pr_author"), _b("📊 Статус", "c:/pr_status")],
                [_b("📈 Итоги недели", "c:/pr_report"), _b("🧪 Проверить источники", "c:/pr_test")],
                [_b("⏸ Пауза отправки", "c:/pr_pause"), _b("▶️ Продолжить", "c:/pr_resume")],
                [_b("📚 Источники", "c:/pr_src list"), _b("👤 Аккаунт менеджера", "c:/pr_login")],
                [_b("📧 Проверить почту", "c:/pr_mailtest")], back]
    elif name == "scan":
        rows = [[_b("Быстрая (час)", "c:/seo_scan hourly"), _b("Новые страницы", "c:/seo_scan new")],
                [_b("Ежедневная", "c:/seo_scan daily"), _b("Недельный отчёт", "c:/seo_scan weekly")],
                [_b("Разбор за месяц", "c:/seo_scan monthly")], [_b("« Назад", "m:seo")]]
    elif name == "sys":
        rows = [[_b("💸 Расходы за месяц", "c:/spend")],
                [_b("📏 Выученные правила ▸", "m:rules")],
                [_b("🔗 Ссылка на офис", "c:/office")],
                [_b("↩️ Откатить версию системы", "m:rollback")], back]
    elif name == "rules":
        rows = [[_b("🎨 Дизайнер", "c:/rules designer"), _b("🖥 Веб", "c:/rules web")],
                [_b("📣 SMM", "c:/rules smm"), _b("🔎 SEO", "c:/rules seo")], [_b("« Назад", "m:sys")]]
    elif name == "rollback":
        rows = [[_b("Да, вернуть предыдущую версию", "c:/rollback")], [_b("« Отмена", "m:sys")]]
    else:
        rows = [back]
    return InlineKeyboardMarkup(inline_keyboard=rows)


MENU_TITLES = {"main": HELP, "seo": "🔎 SEO-шник", "pr": "🤝 PR-агент: ищет авторов на vc.ru и Хабре, дайджест в 10:00", "scan": "🔍 Какую проверку запустить?", "sys": "⚙️ Система",
               "rules": "📏 Правила, выученные из твоих правок — чьи показать?",
               "rollback": "↩️ Вернуть предыдущую версию системы? Нужно, только если после обновления что-то сломалось."}
ASK = {"article": "✍️ Тема статьи? Можно добавить ссылки на работы авторов, которые стоит показать.",
       "seo_fix": "🛠 Пришли адрес статьи — SEO-шник подготовит title, description, разметку, alt и ссылки.",
       "seo_page": "📄 Пришли адрес страницы — быстрая проверка без ИИ, бесплатно.",
       "ws": "📈 Фраза для Вордстата. Несколько через « | » — сравню их.",
       "ws_parse": "🧲 По какой фразе собрать семантику? Фразы лягут в базу SEO-шника.",
       "pr_author": "🔗 Пришли ссылку на статью с vc.ru или Хабра — оценю, найду контакты и напишу черновик.",
       "geo_check": "🤖 Какой запрос проверить в нейроответах? Покажу ответ, источники, есть ли мы и нашу позицию в выдаче."}
HINTS = {"wordstat": "Просто пришли сюда файл CSV или XLSX из Вордстата — фразы попадут в базу SEO-шника."}
CMDS = {"seo_plan": "seo_plan", "seo_status": "seo_status", "seo_scan": "seo_scan", "seo_bulk": "seo_bulk",
        "seo_stop": "seo_stop", "spend": "spend_cmd", "rules": "rules", "office": "office_link", "rollback": "rollback",
        "article": "article", "seo_fix": "seo_fix", "seo_page": "seo_page", "seo_auto": "seo_auto", "seo_tips": "seo_tips",
        "ws": "ws_cmd", "ws_parse": "ws_parse", "ws_map": "ws_map", "ws_hubs": "ws_hubs", "topics_traffic": "topics_traffic", "hub_inventory": "hub_inventory", "hub_classify": "hub_classify", "cluster": "cluster_cmd", "cluster_pick": "cluster_pick",
        "geo_check": "geo_check", "geo_q": "geo_q", "geo_report": "geo_report", "geo_fix": "geo_fix",
        "pr_digest": "pr_digest", "pr_scan": "pr_scan", "pr_status": "pr_status", "pr_test": "pr_test", "pr_src": "pr_src",
        "pr_author": "pr_author", "pr_report": "pr_report", "pr_pause": "pr_pause", "pr_resume": "pr_resume",
        "pr_login": "pr_login", "pr_mailtest": "pr_mailtest", "pr_outreach": "pr_outreach", "pr_stop": "pr_stop"}


async def run_cmd(m: Message, user, text: str):
    """Выполнить команду так, будто её набрали руками (обработчики читают аргументы из m.text)."""
    fake = m.model_copy(update={"text": text, "from_user": user})
    name = text[1:].split()[0]
    await globals()[CMDS[name]](fake)


@dp.callback_query(F.data.regexp(r"^[mactih]:"))
async def menu_cb(c: CallbackQuery):
    if not is_owner(c):
        return
    kind, _, arg = c.data.partition(":")
    uid = c.from_user.id
    if kind == "m":
        try:
            await c.message.edit_text(MENU_TITLES.get(arg, "Меню"), reply_markup=menu_kb(arg))
        except Exception:
            pass   # «message is not modified»
    elif kind == "a":
        NEXT_AGENT[uid] = arg
        WAIT_INPUT.pop(uid, None)
        await c.message.answer(f"{ICON[arg]} {AGENTS[arg]['title']}: пиши задачу — следующее сообщение уйдёт ему "
                               "(можно картинку с подписью).")
    elif kind == "i":
        WAIT_INPUT[uid] = arg
        NEXT_AGENT.pop(uid, None)
        await c.message.answer(ASK[arg])
    elif kind == "h":
        await c.message.answer(HINTS[arg])
    elif kind == "t":
        from core.seo import author_tips
        if arg == "auto":
            await run_cmd(c.message, c.from_user, "/seo_auto " + ("off" if seo_monitor.autopilot() else "on"))
        else:
            await run_cmd(c.message, c.from_user, "/seo_tips " + ("off" if author_tips.enabled() else "on"))
        try:
            await c.message.edit_reply_markup(reply_markup=menu_kb("seo"))
        except Exception:
            pass
    elif kind == "c":
        await c.answer()
        await run_cmd(c.message, c.from_user, arg)
        return
    await c.answer()


@dp.message(F.text == "📋 Меню")
async def menu_btn(m: Message):
    if is_owner(m):
        NEXT_AGENT.pop(m.from_user.id, None)
        WAIT_INPUT.pop(m.from_user.id, None)
        await m.answer(HELP, reply_markup=menu_kb("main"))


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
    if uid in WAIT_OWN and not m.text.startswith("/"):
        fid, _ = WAIT_OWN.pop(uid)
        await ask_media(fid, tg_html.clean(m.html_text)[0], m.answer)   # html_text сохраняет твой жирный/курсив
        return
    if m.reply_to_message and not m.text.startswith("/"):
        hit = smm_flow.by_msg(m.reply_to_message.message_id)
        if hit:
            await rework_variant(hit[0], hit[1], m.text.strip(), m.answer)
            return
    if uid in WAIT_INPUT and not m.text.startswith("/"):
        await run_cmd(m, m.from_user, f"/{WAIT_INPUT.pop(uid)} {m.text}")
        return
    if uid in NEXT_AGENT and not m.text.startswith("/"):
        await _start(m, NEXT_AGENT.pop(uid), m.text.strip())
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


# ---------- пост SMM: 3 текста → 3 картинки → готовый пост ----------
from core import smm_flow  # noqa: E402
from core import tg_html  # noqa: E402


async def send_post(chat, text: str, img: str | None = None, head: str = "", reply_markup=None):
    """Пост с HTML-форматированием (жирный, курсив, ссылки…). Картинка + подпись, если влезает в 1024 знака.
    Если Telegram не принял разметку — отправляем тем же текстом без неё. Возвращает последнее сообщение."""
    import html as _h
    body = (_h.escape(head) if head else "") + text

    vid = bool(img) and str(img).lower().endswith(".mp4")
    send_media = BOT.send_animation if vid else BOT.send_photo

    async def _go(t: str, mode):
        if img and Path(img).exists() and tg_html.visible_len(t) <= 1024:
            return await send_media(chat, FSInputFile(img), caption=t, parse_mode=mode, reply_markup=reply_markup)
        if img and Path(img).exists():
            await send_media(chat, FSInputFile(img))
        return await BOT.send_message(chat, t, parse_mode=mode, reply_markup=reply_markup,
                                      link_preview_options=LinkPreviewOptions(is_disabled=bool(img)))
    try:
        return await _go(body, "HTML")
    except Exception as e:
        if "parse" not in str(e).lower() and "entit" not in str(e).lower():
            raise
        return await _go(tg_html.visible(body), None)


async def send_variants(job: dict):
    meta = job.get("meta") or {}
    brief = dict(meta.get("brief") or {})
    parent = smm_flow.by_rework(job["id"]) or {}
    for k in ("image_title", "format", "channel", "shots", "task"):
        if not brief.get(k) and parent.get(k):
            brief[k] = parent[k]
    fid = smm_flow.new(task=brief.pop("task", None) or job["task"], smm_job=job["id"], variants=meta["variants"], **brief)
    msgs = {}
    for uid in config.OWNER_IDS:
        await BOT.send_message(uid, f"📣 SMM-щик: {job['caption'][:1500]}\n\n💸 ${job['cost']:.3f}\n\n"
                                    "Выбери текст — «✅ Беру». Поправить самому — «✏️ Свой текст». "
                                    "Попросить SMM-щика что-то поменять — ответь на вариант сообщением.")
        for k, text in meta["variants"].items():
            kb = InlineKeyboardMarkup(inline_keyboard=[[_b("✅ Беру этот", f"sv:{fid}:{k}"),
                                                        _b("✏️ Свой текст", f"se:{fid}:{k}")]])
            sent = await send_post(uid, text, head=f"{smm_flow.STYLE_ICON[k]} {smm_flow.STYLES[k]}\n\n", reply_markup=kb)
            msgs[str(sent.message_id)] = k
    smm_flow.update(fid, msgs=msgs)


async def ask_media(fid: str, text: str, say):
    """Текст выбран → что делаем к посту: 3 картинки, ролик или без медиа."""
    flow = smm_flow.get(fid)
    if not flow:
        await say("Этот выбор устарел — дай задачу SMM-щику заново.")
        return
    smm_flow.update(fid, text=text)
    j = office.get_job(flow["smm_job"]) if flow.get("smm_job") else None
    if j and j["status"] == "review":
        runner.approve(j["id"])
    await say("Текст принят. Что делаем к посту?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [_b("🖼 3 картинки на выбор", f"sm:{fid}:image"), _b("🎬 Ролик (видео/гифка)", f"sm:{fid}:video")],
        [_b("📝 Без картинки", f"sm:{fid}:none")]]))


async def go_design(fid: str, media: str, say):
    flow = smm_flow.get(fid)
    if not flow:
        await say("Этот выбор устарел — дай задачу SMM-щику заново.")
        return
    if media == "none":
        smm_flow.update(fid, image=None, media="none")
        await send_final(fid)
        return
    if flow.get("designer_job") and office.get_job(flow["designer_job"]) and \
            office.get_job(flow["designer_job"])["status"] in ("queued", "running"):
        await say("Дизайнер уже работает над этим постом — дождись результата.")
        return
    flow = smm_flow.update(fid, media=media)
    what = "Ролик к посту" if media == "video" else "3 картинки к посту"
    jid = runner.start_job("designer", f"{what}: {flow.get('image_title') or flow['task']}"[:200],
                           source="telegram", full_task=smm_flow.designer_task(flow))
    smm_flow.update(fid, designer_job=jid)
    await say("🎬 Дизайнер делает ролик — это пара минут, пришлю." if media == "video"
              else "🎨 Дизайнер делает 3 картинки — пришлю на выбор.")


async def rework_variant(fid: str, style: str, comment: str, say):
    flow = smm_flow.get(fid)
    if not flow:
        await say("Этот вариант устарел — дай задачу SMM-щику заново.")
        return
    name = smm_flow.STYLES[style]
    jid = runner.start_job("smm", f"Правка варианта «{name}»: {comment}"[:200], source="telegram", full_task=(
        f"Перепиши вариант поста «{name}» по комментарию владельца и сдай через draft_variants ТОЛЬКО поле {style}.\n"
        f"Комментарий: {comment}\n\nТекущий текст:\n{flow['variants'].get(style, '')}\n\nИсходная задача: {flow['task']}"))
    smm_flow.update(fid, rework_jobs=(flow.get("rework_jobs") or []) + [jid])
    await say(f"📣 Передал SMM-щику: переписать «{name}». Пришлю новый вариант.")


def _flow_for_designer(job: dict):
    hit = smm_flow.by_designer_job(job["id"])
    if not hit and job.get("parent"):
        hit = smm_flow.by_designer_job(job["parent"])   # «Переделать» картинок — новая задача того же сценария
        if hit:
            smm_flow.update(hit[0], designer_job=job["id"])
    return hit


async def send_image_choice(job: dict):
    fid, flow = _flow_for_designer(job) or smm_flow.by_designer_job(job["id"])
    vids = [str(config.OUTBOX / f) for f in job["files"] if f.lower().endswith(".mp4")][:3]
    imgs = vids or [str(config.OUTBOX / f) for f in job["files"] if f.lower().endswith(IMG)][:6]
    smm_flow.update(fid, images=imgs)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_b("Беру" if len(imgs) == 1 else f"Беру {i + 1}", f"si:{fid}:{i}") for i in range(len(imgs))],
        [_b("🔁 Переделать ролик" if vids else "🔁 Переделать картинки", f"redo:{job['id']}")],
        [_b("🔄 Лучше картинки" if vids else "🔄 Лучше ролик", f"sm:{fid}:{'image' if vids else 'video'}")]])
    for uid in config.OWNER_IDS:
        if vids:
            for v in vids:
                MSG_JOB[(await BOT.send_animation(uid, FSInputFile(v))).message_id] = job["id"]
        elif len(imgs) == 1:
            MSG_JOB[(await BOT.send_photo(uid, FSInputFile(imgs[0]))).message_id] = job["id"]
        elif imgs:
            for sent in await BOT.send_media_group(uid, [InputMediaPhoto(media=FSInputFile(f), caption=str(i + 1))
                                                         for i, f in enumerate(imgs)]):
                MSG_JOB[sent.message_id] = job["id"]
        sent = await BOT.send_message(uid, f"🎨 {job['caption'][:1200]}\n\n💸 ${job['cost']:.3f}\n\n"
                                           + ("Берём этот ролик? " if vids else "Какую картинку берём? Номер — по порядку в альбоме. ")
                                           + "Правка — ответом на это сообщение.", reply_markup=kb)
        MSG_JOB[sent.message_id] = job["id"]


async def send_final(fid: str):
    flow = smm_flow.get(fid)
    ch = flow.get("channel") or ""
    rows = [[_b(f"🚀 Опубликовать в {ch}", f"sp:{fid}")]] if ch else []
    rows.append([_b("🖼 Сделать картинки", f"sm:{fid}:image"), _b("🎬 Сделать ролик", f"sm:{fid}:video")]
                if flow.get("media") == "none" else [_b("↩️ Другой вариант", f"sc:{fid}")])
    note = "" if ch else "\n\n(канал не задан — это готовый черновик, перешли его в нужный канал)"
    for uid in config.OWNER_IDS:
        await send_post(uid, flow["text"], flow.get("image"))
        await BOT.send_message(uid, "✅ Готовый пост." + note, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data.regexp(r"^s[veicpm]:"))
async def smm_cb(c: CallbackQuery):
    if not is_owner(c):
        return
    kind, fid, *rest = c.data.split(":")
    flow = smm_flow.get(fid)
    if not flow:
        await c.answer("Устарело")
        return
    await c.answer()
    if kind == "sv":
        await ask_media(fid, flow["variants"][rest[0]], c.message.answer)
    elif kind == "sm":
        await go_design(fid, rest[0], c.message.answer)
    elif kind == "se":
        WAIT_OWN[c.from_user.id] = (fid, rest[0])
        await c.message.answer("✏️ Пришли текст поста целиком — можно скопировать вариант выше и поправить. Возьму как есть.")
    elif kind == "si":
        i = int(rest[0])
        imgs = flow.get("images") or []
        if i >= len(imgs):
            return
        smm_flow.update(fid, image=imgs[i])
        j = office.get_job(flow.get("designer_job") or "")
        if j and j["status"] == "review":
            runner.approve(j["id"])
        await send_final(fid)
    elif kind == "sc":
        imgs = flow.get("images") or []
        await c.message.answer("Какой вариант берём?" if len(imgs) > 1 else "Варианты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_b(f"Беру {i + 1}", f"si:{fid}:{i}") for i in range(len(imgs))],
            [_b("🖼 Картинки", f"sm:{fid}:image"), _b("🎬 Ролик", f"sm:{fid}:video"), _b("📝 Без картинки", f"sm:{fid}:none")]]))
    elif kind == "sp":
        try:
            await send_post(flow["channel"], flow["text"], flow.get("image"))
            smm_flow.update(fid, published=time.time())
            await c.message.edit_reply_markup(reply_markup=None)
            await c.message.answer(f"🚀 Опубликовано в {flow['channel']}")
        except Exception as e:
            await c.message.answer(f"Не опубликовал в {flow['channel']}: {e}\nБот должен быть админом канала.")


async def send_review(job: dict):
    """Готовая работа → владельцам в Telegram: картинки альбомом, документы файлами, посты — превью."""
    if not BOT:
        return
    if job["agent"] == "smm" and (job.get("meta") or {}).get("variants"):
        await send_variants(job)
        return
    if job["agent"] == "designer" and _flow_for_designer(job):
        await send_image_choice(job)
        return
    if (config.SEO_AUTO_PLANS and job["agent"] == "seo" and job.get("source") == "content_plan"
            and (job.get("meta") or {}).get("stage") == "plan"):
        runner.approve(job["id"])   # план из утверждённого контент-плана — сразу в работу, без согласования
        return
    files = [config.OUTBOX / f for f in job["files"]]
    imgs = [f for f in files if f.suffix.lower() in IMG and f.exists()][:10]
    docs = [f for f in files if f.suffix.lower() not in IMG and f.exists() and f.name != "posts.md"][:5]
    posts = (job.get("meta") or {}).get("posts") or []
    if any(p.get("image") for p in posts):
        # у SMM показываем то, что уйдёт в канал: картинка вместе с текстом поста, без рабочих материалов
        imgs = []
    imgs = [f for f in imgs if not f.name.startswith("figma-")]
    vids = [f for f in files if f.suffix.lower() == ".mp4" and f.exists()][:4]
    docs = [d for d in docs if d.suffix.lower() not in (".mp4", ".json")]
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
        for v in vids:
            MSG_JOB[(await BOT.send_animation(uid, FSInputFile(v))).message_id] = job["id"]
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
            sent = await send_post(uid, p["text"], p.get("image"), head=f"📝 Пост {i} → {p.get('channel') or 'черновик'}\n\n")
            MSG_JOB[sent.message_id] = job["id"]
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
            await send_post(ch, p["text"], p.get("image"))
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
    asyncio.create_task(pr_jobs.loop())
    if not config.TELEGRAM_BOT_TOKEN:
        logging.warning("Нет TELEGRAM_BOT_TOKEN — работает только офис")
        await asyncio.Event().wait()
    BOT = Bot(config.TELEGRAM_BOT_TOKEN)
    pr_tg.setup(BOT)
    try:
        from aiogram.types import BotCommand
        await BOT.set_my_commands([BotCommand(command="menu", description="Меню"),
                                   BotCommand(command="office", description="Ссылка на ИИ-офис"),
                                   BotCommand(command="spend", description="Расходы за месяц")])
        if office_url():
            # синяя кнопка слева от поля ввода открывает ИИ-офис прямо в Telegram (мини-апп)
            for uid in config.OWNER_IDS:
                await BOT.set_chat_menu_button(chat_id=uid, menu_button=MenuButtonWebApp(
                    text="Офис", web_app=WebAppInfo(url=office_url())))
    except Exception:
        logging.exception("не удалось настроить меню бота")
    runner.on_review(send_review)
    runner.on_approved(publish_posts)
    runner.on_approved(seo_after_approve)
    await dp.start_polling(BOT)


if __name__ == "__main__":
    asyncio.run(main())
