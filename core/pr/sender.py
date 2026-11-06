"""Отправка с аккаунта PR/sales-менеджера (Telethon) и приём ответов (ТЗ, раздел 6).

Сообщение уходит только после «✅ Отправить» владельца. Ограничения:
- окно PR_SEND_WINDOW (10-19 МСК), пауза между отправками 3–10 мин;
- новых диалогов в день: PR_WARMUP_LIMIT (5) первые PR_WARMUP_DAYS (14) дней, потом PR_DAILY_LIMIT (10);
- одному человеку — одно первое сообщение и одно напоминание; стоп-лист навсегда;
- PeerFlood / долгий FloodWait / ограничение от @SpamBot → вся отправка на паузу, алерт владельцу.

Ответ человека → владельцу в бот: кто, по какой статье, что написал. Дальше переписку ведёт владелец/менеджер.
Вход в аккаунт (один раз): sudo -u agents /home/agents/dsgners-agents/.venv/bin/python -m core.pr.login
"""
import asyncio
import datetime as dt
import logging
import random
import re
import smtplib
import time
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from core import config
from core.pr import store

log = logging.getLogger("pr.sender")

API_ID = int(config.env("TG_API_ID", "0") or 0)
API_HASH = config.env("TG_API_HASH", "")
SESSION = config.DATA / (config.env("TG_SESSION", "pr_sender").removesuffix(".session") or "pr_sender")
WINDOW = tuple(int(x) for x in config.env("PR_SEND_WINDOW", "10-19").split("-"))
DAILY = int(config.env("PR_DAILY_LIMIT", "10"))
WARMUP = int(config.env("PR_WARMUP_LIMIT", "5"))
WARMUP_DAYS = int(config.env("PR_WARMUP_DAYS", "14"))
GAP = (int(config.env("PR_GAP_MIN", "180")), int(config.env("PR_GAP_MAX", "600")))
RESOLVE_PER_DAY = int(config.env("PR_RESOLVE_PER_DAY", "40"))
SMTP = {k: config.env(f"SMTP_{k}", "") for k in ("HOST", "PORT", "USER", "PASSWORD", "FROM")}
# входящие (ответы авторов): по умолчанию тот же ящик; smtp.yandex.ru → imap.yandex.ru
IMAP_HOST = config.env("IMAP_HOST", "") or (SMTP["HOST"].replace("smtp.", "imap.", 1) if SMTP["HOST"].startswith("smtp.") else "")
IMAP_PORT = int(config.env("IMAP_PORT", "993") or 993)
MAIL_POLL = int(config.env("PR_MAIL_POLL", "180"))
REFUSAL = re.compile(r"(?i)не интерес|неинтерес|не надо|не нужно|не пиши|не пишите|нет,? спасибо|отпиш|спам|не беспокой|\bstop\b|"
                     r"удалите|отстань")

_client = None
_me = None
_say = None
_tasks: list = []


def setup(say):
    global _say
    _say = say


async def _notify(text: str):
    if _say:
        try:
            await _say(text)
        except Exception:
            log.exception("notify")


def ready() -> bool:
    return _client is not None


def mail_ready() -> bool:
    return bool(SMTP["HOST"] and SMTP["USER"] and SMTP["PASSWORD"])


def me() -> str:
    if not _me:
        return ""
    return f"{_me.first_name or ''} @{_me.username or '—'}".strip()


def paused() -> str:
    return store.get("send_paused", "") or ""


def pause(reason: str):
    store.put("send_paused", reason or "вручную")


def resume():
    store.put("send_paused", "")


def _now() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(config.SEO_TZ))


def _today_start() -> float:
    n = _now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def limit_today() -> int:
    first = store.first_sent_ts()
    return WARMUP if not first or time.time() - first < WARMUP_DAYS * 86400 else DAILY


def sent_today() -> int:
    return store.sent_since(_today_start())


# ---------- подключение ----------

async def start():
    """Цикл отправки (Telegram и почта) и чтение ответов с почты — всегда; Telegram — если вход уже сделан."""
    _tasks.append(asyncio.create_task(_send_loop()))
    if mail_ready() and IMAP_HOST:
        _tasks.append(asyncio.create_task(_mail_loop()))
    await _start_tg()


async def _start_tg():
    global _client, _me
    if not API_ID or not API_HASH:
        log.info("PR: нет TG_API_ID/TG_API_HASH — отправка выключена")
        return
    if not SESSION.with_suffix(".session").exists():
        log.info("PR: нет сессии %s — нужен вход: python -m core.pr.login", SESSION)
        return
    from telethon import TelegramClient, events
    c = TelegramClient(str(SESSION), API_ID, API_HASH, device_model="dsgners-agents", system_version="VPS")
    try:
        await c.connect()
        if not await c.is_user_authorized():
            log.warning("PR: сессия не авторизована — повтори вход")
            await c.disconnect()
            day = time.strftime("%Y-%m-%d")
            if store.get("unauth_alert") != day:          # не чаще раза в сутки — бот перезапускается при обновлениях
                store.put("unauth_alert", day)
                await _notify("⚠️ PR: вход в аккаунт менеджера начат, но не завершён (не введён код или облачный пароль). "
                              "Отбор статей и дайджест работают, отправка — нет. Закончить вход: /pr_login")
            return
    except Exception:
        log.exception("PR: не подключился к Telegram")
        return
    _client = c
    _me = await c.get_me()
    c.add_event_handler(_on_incoming, events.NewMessage(incoming=True))
    log.info("PR: аккаунт отправки %s", me())


# ---------- проверка @ ----------

async def resolve(username: str) -> tuple[str, str]:
    """(ok|dead|bot|channel|unchecked, пояснение)."""
    if not _client:
        return "unchecked", "аккаунт отправки не подключён"
    day = _now().strftime("%Y-%m-%d")
    used = store.get("resolve_count", {})
    if used.get(day, 0) >= RESOLVE_PER_DAY:
        return "unchecked", "лимит проверок на сегодня"
    store.put("resolve_count", {day: used.get(day, 0) + 1})
    from telethon import errors, types
    try:
        e = await _client.get_entity(username)
    except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
        return "dead", "такого @ нет"
    except errors.FloodWaitError as ex:
        return "unchecked", f"Telegram просит подождать {ex.seconds} с"
    except Exception as ex:
        return "unchecked", str(ex)[:100]
    if isinstance(e, types.User):
        if e.bot:
            return "bot", "это бот"
        if e.deleted:
            return "dead", "аккаунт удалён"
        return "ok", " ".join(filter(None, [e.first_name, e.last_name])) or "есть"
    return "channel", "канал или чат — написать нельзя"


# ---------- отправка ----------

async def _send_loop():
    while True:
        try:
            await _tick()
        except Exception:
            log.exception("PR send loop")
        await asyncio.sleep(30)


async def _tick():
    if paused():
        return
    now = _now()
    if not (WINDOW[0] <= now.hour < WINDOW[1]):
        return
    if time.time() < float(store.get("next_send", 0) or 0):
        return
    for o in store.outreach_by("approved", 50):
        if o["channel"] == "tg" and not _client:
            continue                              # ждёт входа в аккаунт менеджера
        if o["channel"] == "email" and not mail_ready():
            continue
        a = store.article(o["article_id"]) or {}
        if store.stopped(o["value"], a.get("author_url", "")):
            store.set_outreach(o["id"], status="stopped", note="в стоп-листе")
            continue
        if o["kind"] == "first":
            if store.already_contacted_other(o["id"], o["value"], a.get("author_url", "")):
                store.set_outreach(o["id"], status="skipped", note="этому человеку уже писали")
                continue
            if sent_today() >= limit_today():
                return
        await _send(o, a)
        store.put("next_send", time.time() + random.randint(*GAP))
        return


async def _send(o: dict, a: dict):
    if o["channel"] == "tg":
        await _send_tg(o, a)
    elif o["channel"] == "email":
        await _send_mail(o, a)
    else:
        store.set_outreach(o["id"], status="failed", note="этот канал — только вручную")


async def _send_tg(o: dict, a: dict):
    from telethon import errors
    try:
        msg = await _client.send_message(o["value"], o["text"])
    except errors.PeerFloodError:
        pause("Telegram ограничил отправку (PeerFlood)")
        await _notify("⛔ PR: Telegram ограничил отправку с аккаунта менеджера (PeerFlood). Отправку поставил на паузу.\n"
                      "Проверь @SpamBot в аккаунте менеджера. Вернуть: /pr_resume — не раньше чем через сутки.")
        return
    except errors.FloodWaitError as ex:
        if ex.seconds > 900:
            pause(f"FloodWait {ex.seconds} с")
            await _notify(f"⛔ PR: Telegram просит подождать {ex.seconds // 60} мин. Отправка на паузе, /pr_resume — потом.")
        else:
            store.put("next_send", time.time() + ex.seconds + 60)
        return
    except (errors.UserPrivacyRestrictedError, errors.UserIsBlockedError, errors.InputUserDeactivatedError,
            errors.YouBlockedUserError, ValueError) as ex:
        store.set_outreach(o["id"], status="failed", note=type(ex).__name__)
        await _notify(f"⚠️ PR: не отправилось @{o['value']} — {_why(ex)}. Статья: «{a.get('title', '')}»")
        return
    except Exception as ex:
        store.set_outreach(o["id"], status="failed", note=str(ex)[:200])
        await _notify(f"⚠️ PR: не отправилось @{o['value']}: {ex}")
        return
    peer = getattr(msg.peer_id, "user_id", None)
    store.set_outreach(o["id"], status="sent", sent=time.time(), msg_id=msg.id, peer_id=peer)
    await _notify(f"✉️ Отправил @{o['value']}" + (" (напоминание)" if o["kind"] == "followup" else "")
                  + f" — по статье «{a.get('title', '')}». Сегодня {sent_today()}/{limit_today()}.")


def _why(ex) -> str:
    return {"UserPrivacyRestrictedError": "у человека закрыты сообщения от незнакомых",
            "UserIsBlockedError": "он нас заблокировал", "YouBlockedUserError": "мы его заблокировали",
            "InputUserDeactivatedError": "аккаунт удалён"}.get(type(ex).__name__, str(ex)[:100])


def _smtp_send(to: str, subject: str, body: str) -> str:
    import email.utils as eu
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = SMTP["FROM"] or SMTP["USER"], to, subject
    m["Date"] = eu.formatdate(localtime=True)
    mid = eu.make_msgid(domain=(SMTP["USER"].split("@")[-1] or "dsgners.ru"))
    m["Message-ID"] = mid
    m["Reply-To"] = SMTP["USER"]
    m.set_content(body)
    port = int(SMTP["PORT"] or 465)
    cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with cls(SMTP["HOST"], port, timeout=30) as s:
        if port != 465:
            s.starttls()
        s.login(SMTP["USER"], SMTP["PASSWORD"])
        s.send_message(m)
    return mid


async def _send_mail(o: dict, a: dict):
    body = o["text"] + "\n\n—\nЕсли не хотите писем — просто ответьте «нет», больше не напишем."
    subject = f"Ваша статья «{a.get('title', '')[:80]}» и дизайнерс" if a.get("track") != "company" else \
        f"Кейсы {a.get('author', '') or 'компании'} на дизайнерс"
    try:
        mid = await asyncio.to_thread(_smtp_send, o["value"], subject, body)
    except Exception as ex:
        store.set_outreach(o["id"], status="failed", note=str(ex)[:200])
        await _notify(f"⚠️ PR: письмо {o['value']} не ушло: {ex}")
        return
    store.set_outreach(o["id"], status="sent", sent=time.time(), mail_id=mid)
    await _notify(f"📧 Письмо {o['value']} — по статье «{a.get('title', '')}». Сегодня {sent_today()}/{limit_today()}.\n"
                  "Ответ перешлю сюда.")


# ---------- ответы ----------

async def _on_incoming(event):
    if not event.is_private or not event.sender_id:
        return
    o = store.outreach_by_peer(event.sender_id)
    if not o:
        return                                   # чужой диалог менеджера — не трогаем
    a = store.article(o["article_id"]) or {}
    sender = await event.get_sender()
    who = " ".join(filter(None, [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")])) or a.get("author", "")
    at = f"@{sender.username}" if getattr(sender, "username", None) else f"@{o['value']}"
    text = (event.raw_text or "").strip() or "[вложение без текста]"
    await _reply(o, a, who, at, text, "в аккаунте менеджера")


async def _reply(o: dict, a: dict, who: str, at: str, text: str, where: str):
    """Ответ автора → владельцу: кто, по какой статье, что мы писали и что он ответил. Отказ → стоп-лист."""
    first = o["status"] != "replied"
    store.set_outreach(o["id"], status="replied", replied=o["replied"] or time.time())
    refused = bool(REFUSAL.search(text)) and len(text) < 300
    if refused:
        store.stop(o["value"], "отказался")
        if a.get("author_url"):
            store.stop(a["author_url"], "отказался")
    sent = dt.datetime.fromtimestamp(o["sent"], ZoneInfo(config.SEO_TZ)).strftime("%d.%m") if o["sent"] else "?"
    if first:
        head = (f"💬 Ответил автор — {who} ({at})\n"
                f"Статья: «{a.get('title', '')}» ({'Хабр' if a.get('site') == 'habr' else a.get('site', '')})\n{a.get('url', '')}\n"
                f"Мы писали {sent}: «{o['text'][:160]}…»\n\nОтвет:\n{text[:3000]}")
        tail = ("\n\n🚫 Похоже на отказ — внёс в стоп-лист, больше не пишем." if refused else
                f"\n\nДальше переписка твоя — {where}. Агент в этот диалог больше не пишет.")
        await _notify(head + tail)
    else:
        await _notify(f"💬 {who} ({at}) ещё пишет — по статье «{a.get('title', '')}»:\n{text[:3000]}"
                      + ("\n\n🚫 Похоже на отказ — внёс в стоп-лист." if refused else ""))


# ---------- прочитано / напоминание / @SpamBot ----------

async def check_read() -> list[dict]:
    """Отметить прочитанные; вернуть отправленные ≥ PR_FOLLOWUP_DAYS назад, прочитанные и без ответа."""
    if not _client:
        return []
    from telethon import functions
    days = float(config.env("PR_FOLLOWUP_DAYS", "7"))
    due = []
    for status in ("sent", "read"):
        for o in store.outreach_by(status, 200):
            if o["kind"] != "first" or not o["peer_id"] or not o["msg_id"]:
                continue
            try:
                peer = await _client.get_input_entity(o["peer_id"])
                r = await _client(functions.messages.GetPeerDialogsRequest(peers=[peer]))
                read = r.dialogs and r.dialogs[0].read_outbox_max_id >= o["msg_id"]
            except Exception as ex:
                log.info("read check %s: %s", o["value"], ex)
                continue
            if read and status == "sent":
                store.set_outreach(o["id"], status="read", read=time.time())
            if read and time.time() - (o["sent"] or 0) >= days * 86400 and not store.has_followup(o["article_id"]):
                due.append(store.outreach(o["id"]))
            await asyncio.sleep(1)
    return due


async def spambot() -> str:
    """Спросить @SpamBot, нет ли ограничений. Есть — пауза и алерт."""
    if not _client:
        return "аккаунт не подключён"
    try:
        async with _client.conversation("SpamBot", timeout=30) as conv:
            await conv.send_message("/start")
            r = await conv.get_response()
        t = r.raw_text or ""
    except Exception as ex:
        return f"не проверил: {ex}"
    bad = re.search(r"(?i)limited|ограничен|restricted", t) and not re.search(r"(?i)no limits|good news|свободен|нет ограничений", t)
    store.put("spambot", {"ts": time.time(), "ok": not bad, "text": t[:300]})
    if bad:
        pause("@SpamBot: аккаунт ограничен")
        await _notify("⛔ PR: @SpamBot пишет, что аккаунт менеджера ограничен. Отправка на паузе.\n\n" + t[:800])
        return "ограничен — пауза"
    return "без ограничений"


# ---------- почта: ответы и отказы доставки ----------

def _plain(msg) -> str:
    """Текст письма без цитаты нашего сообщения."""
    import html as h
    part = None
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain" and not p.get_filename():
                part = p
                break
        if part is None:
            for p in msg.walk():
                if p.get_content_type() == "text/html":
                    part = p
                    break
    else:
        part = msg
    if part is None:
        return ""
    try:
        t = part.get_content()
    except Exception:
        t = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        t = h.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?i)<br\s*/?>|</p>", "\n", t)))
    out = []
    for line in t.splitlines():
        if line.strip().startswith(">") or re.match(r"(?i)^\s*(--\s*$|-----\s*original|\d{1,2}[./]\d{1,2}[./]\d{2,4}.*(пишет|wrote|написал)|"
                                                    r"(вт|пн|ср|чт|пт|сб|вс|on|mon|tue|wed|thu|fri|sat|sun)\b.*(пишет|wrote|написал))", line):
            if out and line.strip().startswith(">"):
                continue
            break
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _imap_fetch(last_uid: int) -> tuple[list[tuple[int, bytes]], int]:
    """Новые письма (uid > last_uid) без пометки «прочитано». Первый запуск — только запомнить последний uid."""
    import imaplib
    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30) as m:
        m.login(SMTP["USER"], SMTP["PASSWORD"])
        m.select("INBOX", readonly=True)
        typ, data = m.uid("search", None, f"UID {last_uid + 1}:*" if last_uid else "ALL")
        uids = [int(x) for x in (data[0] or b"").split()]
        uids = [u for u in uids if u > last_uid]
        if not last_uid:
            return [], max(uids or [0])
        out = []
        for u in uids[:50]:
            typ, d = m.uid("fetch", str(u), "(BODY.PEEK[])")
            if d and isinstance(d[0], tuple):
                out.append((u, d[0][1]))
        return out, max(uids or [last_uid])


async def check_mail() -> int:
    """Прочитать новые письма в ящике и переслать ответы авторов владельцу. Возвращает число пересланных."""
    import email as em
    import email.policy
    import email.utils as eu
    last = int(store.get("imap_last_uid", 0) or 0)
    msgs, new_last = await asyncio.to_thread(_imap_fetch, last)
    n = 0
    for uid, raw in msgs:
        msg = em.message_from_bytes(raw, policy=email.policy.default)
        frm_name, frm = eu.parseaddr(str(msg.get("From", "")))
        refs = re.findall(r"<[^>]+>", f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}")
        body = _plain(msg)
        if re.search(r"(?i)mailer-daemon|postmaster", frm):        # письмо не доставлено
            o = store.outreach_by_mail(refs, "") or next(
                (x for x in store.outreach_by("sent", 200) if x["channel"] == "email" and x["value"].lower() in raw.decode("utf-8", "ignore").lower()), None)
            if o:
                store.set_outreach(o["id"], status="failed", note="письмо не доставлено")
                await _notify(f"⚠️ PR: письмо {o['value']} не доставлено — адрес не работает.")
            continue
        o = store.outreach_by_mail(refs, frm)
        if not o:
            continue                                                # чужое письмо в ящике — не трогаем
        a = store.article(o["article_id"]) or {}
        await _reply(o, a, frm_name or a.get("author", ""), frm, body or "[письмо без текста]", f"на почте {SMTP['USER']}")
        n += 1
    store.put("imap_last_uid", new_last)
    return n


async def _mail_loop():
    await asyncio.sleep(20)
    fails = 0
    while True:
        try:
            await check_mail()
            fails = 0
        except Exception as ex:
            fails += 1
            log.warning("PR mail: %s", ex)
            if fails == 3:
                await _notify(f"⚠️ PR: не читается почта {SMTP['USER']} ({IMAP_HOST}): {ex}\n"
                              "Ответы с почты не пересылаются. Проверь пароль приложения и что IMAP включён: /pr_mailtest")
        await asyncio.sleep(MAIL_POLL)


async def mail_test() -> str:
    """Проверка: отправить себе тестовое письмо и зайти в ящик по IMAP."""
    if not mail_ready():
        return "Почта не настроена: в .env нет SMTP_HOST / SMTP_USER / SMTP_PASSWORD."
    lines = [f"Ящик: {SMTP['USER']} · SMTP {SMTP['HOST']}:{SMTP['PORT'] or 465} · IMAP {IMAP_HOST or '—'}:{IMAP_PORT}"]
    try:
        await asyncio.to_thread(_smtp_send, SMTP["USER"], "Проверка PR-агента дизайнерс",
                                "Это тестовое письмо от PR-агента. Если оно пришло — отправка работает.")
        lines.append("✅ Отправка (SMTP): тестовое письмо ушло на этот же ящик")
    except Exception as ex:
        lines.append(f"❌ Отправка (SMTP): {ex}")
    if IMAP_HOST:
        try:
            import imaplib

            def _login():
                with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30) as m:
                    m.login(SMTP["USER"], SMTP["PASSWORD"])
                    m.select("INBOX", readonly=True)
            await asyncio.to_thread(_login)
            lines.append("✅ Чтение (IMAP): вхожу в ящик — ответы авторов буду пересылать сюда")
        except Exception as ex:
            lines.append(f"❌ Чтение (IMAP): {ex} — включи доступ по IMAP в настройках почты")
    else:
        lines.append("⚠️ IMAP не задан (IMAP_HOST) — ответы с почты пересылаться не будут")
    return "\n".join(lines)
