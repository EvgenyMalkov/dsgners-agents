"""Контакты автора — только то, что он сам опубликовал как способ связи (ТЗ, раздел 5).

Откуда: профиль на Хабре/vc.ru, ссылки в тексте статьи рядом со словами «мой канал», «пишите», его Telegram-канал
(контакт из описания канала). Не ищем: телефоны, базы, «пробив», угадывание почты.

Уверенность:
  high — контакт в профиле автора, в его описании канала или ссылка в статье рядом с «мой/моём/подписывайтесь/пишите»;
  low  — просто ссылка в тексте статьи (может быть чужой канал). В отправку идут только high.

Проверка Telegram: аккаунт отправки (sender.resolve) — существует ли, это человек, канал или бот.
"""
import asyncio
import logging
import re
import socket
import time

from core import config
from core.pr import sources, store

log = logging.getLogger("pr.contacts")

HABR_WHOIS = config.env("PR_HABR_WHOIS_API", "https://habr.com/kek/v2/users/{alias}/whois?fl=ru&hl=ru")
VC_SUBSITE = config.env("PR_VC_SUBSITE_API", "https://api.vc.ru/v2.31/subsite?id={id}")

# служебные каналы площадок и сервисов — не контакты автора
SKIP_TG = {"habr", "habr_com", "habrahabr", "habr_career", "vcru", "vc", "vcfeed", "tproger", "dsgners", "share", "joinchat",
           "addstickers", "proxy", "iv", "s", "c", "durov", "telegram", "botfather"}
OWN_CTX = re.compile(r"(?i)\bмо[йеёия]\w*|подпис|пиши(те)?\b|напиш|связ|контакт|личк|автор\w*|\bmy\b|follow|в телеграм")
TG_RE = re.compile(r"(?i)(?:https?://)?(?:t\.me|telegram\.me)/([a-z0-9_]{4,32})(?:[/?]|\b)")
AT_RE = re.compile(r"(?<![\w.@])@([a-z][a-z0-9_]{3,31})\b", re.I)
MAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
VK_RE = re.compile(r"(?i)(?:https?://)?(?:m\.)?vk\.com/([a-z0-9_.]{3,40})")


def _tg(value: str) -> str:
    return value.lower().lstrip("@")


def parse(text: str) -> list[tuple[str, str]]:
    """[(channel, value)] из произвольного текста или ссылки."""
    out = []
    for m in TG_RE.finditer(text or ""):
        if _tg(m.group(1)) not in SKIP_TG:
            out.append(("tg", _tg(m.group(1))))
    for m in AT_RE.finditer(text or ""):
        if _tg(m.group(1)) not in SKIP_TG:
            out.append(("tg", _tg(m.group(1))))
    for m in MAIL_RE.finditer(text or ""):
        e = m.group(0).lower()
        if not e.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")):
            out.append(("email", e))
    for m in VK_RE.finditer(text or ""):
        if m.group(1).lower() not in ("share.php", "away.php", "feed", "im", "wall"):
            out.append(("vk", m.group(1).lower()))
    return list(dict.fromkeys(out))


async def _profile_texts(s, row: dict) -> list[tuple[str, str]]:
    """[(источник, текст)] из профиля автора на площадке."""
    out = []
    url = row.get("author_url") or ""
    if row["site"] == "habr" and "/users/" in url:
        alias = url.rstrip("/").split("/")[-1]
        try:
            j = await sources.fetch(s, HABR_WHOIS.format(alias=alias), want_json=True)
            out.append((f"профиль Хабра {url}", _strings(j)))
        except Exception as e:
            log.info("habr whois %s: %s", alias, e)
    if row["site"] == "vc" and row.get("author_stats", {}).get("vc_id"):
        try:
            j = await sources.fetch(s, VC_SUBSITE.format(id=row["author_stats"]["vc_id"]), want_json=True)
            out.append((f"профиль vc.ru {url}", _strings(j.get("result") or j)))
        except Exception as e:
            log.info("vc subsite: %s", e)
    if url and not out:
        try:
            h = await sources.fetch(s, url)
            desc = " ".join(filter(None, [sources._meta(h, "og:description"), sources._meta(h, "description")]))
            out.append((f"профиль {url}", desc))
        except Exception as e:
            log.info("profile %s: %s", url, e)
    return out


def _strings(j) -> str:
    """Все строки из JSON профиля (описание, контакты, ссылки) одним текстом."""
    parts, stack = [], [j]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
        elif isinstance(o, str) and len(o) < 2000:
            parts.append(o)
    return "\n".join(parts)


async def _channel_owner(s, name: str) -> list[str]:
    """Публичная страница t.me/<канал>: контакты из описания (часто «по вопросам — @me»)."""
    try:
        h = await sources.fetch(s, f"https://t.me/{name}")
    except Exception:
        return []
    desc = sources._meta(h, "og:description") + "\n" + sources.html_to_text(
        (re.search(r'(?is)class="tgme_page_description[^"]*"[^>]*>(.*?)</div>', h) or [None, ""])[1])
    return [v for ch, v in parse(desc) if ch == "tg" and v != name]


async def find(row: dict, a_links: list[dict]) -> list[dict]:
    """Найти и записать контакты автора статьи. Возвращает contacts_for(article)."""
    aid = row["id"]
    async with sources.session() as s:
        for src, text in await _profile_texts(s, row):
            for ch, v in parse(text):
                store.add_contact(aid, ch, v, src, "high")
        for ln in a_links or []:
            found = parse(ln["url"]) or parse(ln["url"].replace("mailto:", ""))
            own = bool(OWN_CTX.search(ln.get("ctx", "")))
            for ch, v in found:
                store.add_contact(aid, ch, v, f"ссылка в статье: «…{ln.get('ctx', '')[-120:]}»", "high" if own else "low")
        # Telegram-канал автора (high) → контакт из описания канала
        for c in store.contacts_for(aid):
            if c["channel"] == "tg" and c["confidence"] == "high":
                for v in await _channel_owner(s, c["value"]):
                    store.add_contact(aid, "tg", v, f"описание канала t.me/{c['value']}", "high")
    return store.contacts_for(aid)


async def validate(cs: list[dict]) -> list[dict]:
    """Telegram — через аккаунт отправки; e-mail — домен существует. Обновляет статус в базе."""
    from core.pr import sender
    for c in cs:
        if c["status"] not in ("unknown", "unchecked"):
            continue
        if c["channel"] == "tg":
            st, note = await sender.resolve(c["value"])
        elif c["channel"] == "email":
            st, note = await _mail_ok(c["value"])
        else:
            st, note = "manual", "отправка вручную"
        store.set_contact(c["id"], status=st, note=note, checked=time.time())
        await asyncio.sleep(0.5)
    return store.contacts_for(cs[0]["article_id"]) if cs else []


async def _mail_ok(email: str) -> tuple[str, str]:
    domain = email.split("@")[-1]
    try:
        await asyncio.get_running_loop().getaddrinfo(domain, 25)
        return "ok", "домен существует"
    except socket.gaierror:
        return "dead", "домена нет"


def best(cs: list[dict], skip=lambda c: False) -> dict | None:
    """Лучший контакт для первого сообщения: Telegram-человек → e-mail → VK. Только high; skip — уже писали/стоп-лист."""
    order = [("tg", "ok"), ("email", "ok"), ("vk", "manual")]
    for ch, st in order:
        for c in cs:
            if c["channel"] == ch and c["confidence"] == "high" and c["status"] == st and not skip(c):
                return c
    return None
