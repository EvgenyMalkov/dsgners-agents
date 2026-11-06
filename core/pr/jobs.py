"""Работа PR-агента: сбор, подготовка карточек, дайджест, напоминания, отчёт и расписание (время — SEO_TZ, Москва).

понедельник 06:00 — перечитать профили авторов дизайнерс (имя, внешние ссылки) — для сверки
07:00 — сбор новых статей за сутки и оценка (тихо)
10:00 — дайджест: до PR_DIGEST_MAX карточек ≥ PR_MIN_SCORE. В карточке: 🆕/💤, контакты, черновик и кнопки
11:00 — кто прочитал и не ответил 7 дней → черновик напоминания на «✅»; @SpamBot; чистка старых контактов
понедельник 10:30 — итоги недели
Отправка — sender.py (окно 10–19, лимиты), ответы авторов — сразу владельцу.
"""
import asyncio
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

from core import config, spend
from core.pr import authors, contacts, outreach, score, sender, sources, store

log = logging.getLogger("pr.jobs")

ENABLED = config.env("PR_ENABLED", "1") == "1"
MIN_SCORE = float(config.env("PR_MIN_SCORE", "7"))
DIGEST_MAX = int(config.env("PR_DIGEST_MAX", "10"))
DIGEST_COMPANIES = int(config.env("PR_DIGEST_COMPANIES", "5"))   # отдельно — блоги компаний
MAX_AGE_DAYS = float(config.env("PR_MAX_AGE_DAYS", "7"))
PER_RUN = int(config.env("PR_PER_RUN", "120"))          # статей разобрать за один сбор
AI_PER_RUN = int(config.env("PR_AI_PER_RUN", "60"))     # из них оценить Claude
SCAN_HOUR = int(config.env("PR_SCAN_HOUR", "7"))
DIGEST_HOUR = int(config.env("PR_DIGEST_HOUR", "10"))
CONTACT_TTL = int(config.env("PR_CONTACT_TTL_DAYS", "90"))
SITE_NAME = {"habr": "Хабр", "vc": "vc.ru"}

_send = None      # async (text, aid | None, oid | None) — в Telegram владельцам с нужными кнопками
_running: set[str] = set()


def setup(send):
    global _send
    _send = send
    sender.setup(lambda t: say(t))


async def say(text: str, aid: int | None = None, oid: int | None = None):
    if _send:
        try:
            await _send(text, aid, oid)
        except Exception:
            log.exception("pr notify failed")


def outreach_on() -> bool:
    v = store.get("outreach_on")
    return (config.env("PR_OUTREACH", "1") == "1") if v is None else bool(v)


def set_outreach_on(v: bool):
    store.put("outreach_on", v)


def _now() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(config.SEO_TZ))


def _date(ts: float) -> dt.date:
    return dt.datetime.fromtimestamp(ts, ZoneInfo(config.SEO_TZ)).date()


def _kind(row: dict, kinds: dict) -> str:
    k = kinds.get(row["source_id"])
    if k:
        return k
    return "habr" if "habr.com" in row["url"] else "vc" if "vc.ru" in row["url"] else "rss"


# ---------- сбор ----------

async def _rate_one(s, row: dict, kind: str, use_ai: bool = True) -> str:
    """Детали → фильтр → сигнал → Claude. Возвращает итоговую стадию."""
    item = sources.Item(url=row["url"], title=row["title"], author=row["author"], published=row["published"] or 0)
    try:
        a = await sources.details(s, kind, item)
    except Exception as e:
        store.update_article(row["id"], stage="error", reason=f"не прочиталась: {e}"[:200])
        return "error"
    reason = score.filter_reason(a, kind)
    sig = score.signal(a)
    ast = dict(a.author_stats, vc_id=a.author_id) if a.author_id else a.author_stats
    store.update_article(row["id"], title=a.title or row["title"], author=a.author, author_url=a.author_url,
                         published=a.published or row["published"], chars=len(a.text), images=a.images,
                         stats=a.stats, author_stats=ast, signal=sig, links=a.links, excerpt=a.text[:4000])
    if reason:
        store.update_article(row["id"], stage="rejected", reason=reason)
        return "rejected"
    if not use_ai:
        return "new"
    r = await score.ai_rate(a)
    track = "company" if a.corporate or r.get("author_kind") in ("company", "school") else "author"
    hist = store.author_history(a.author_url, MIN_SCORE, row["id"])
    auth = score.author_score(a.author_stats, hist)
    store.update_article(row["id"], track=track, author_score=auth)
    if r.get("design_focus", 2) < 2 or not r.get("design", True):
        store.update_article(row["id"], stage="rejected", ai=r["score"], ai_json=r,
                             reason="дизайн — побочная тема (Claude)" if r.get("design_focus") == 1 else "не про дизайн (Claude)")
        return "rejected"
    store.update_article(row["id"], stage="scored", ai=r["score"], ai_json=r, total=score.total(r["score"], sig, auth))
    return "scored"


async def scan() -> str:
    t0 = time.time()
    new_total, feed_err = 0, []
    srcs = store.sources(only_enabled=True)
    kinds = {s["id"]: s["kind"] for s in store.sources()}
    async with sources.session() as s:
        for src in srcs:
            try:
                items = await sources.read_feed(s, src["url"])
                cnt = 0
                for it in items:
                    if it.published and time.time() - it.published > MAX_AGE_DAYS * 86400:
                        continue
                    site = "habr" if "habr.com" in it.url else "vc" if "vc.ru" in it.url else src["kind"]
                    if store.add_article(it.url, site, src["id"], it.title, it.author, it.published):
                        cnt += 1
                new_total += cnt
                store.set_source(src["id"], last_ts=time.time(), last_ok=1, last_count=len(items),
                                 last_note=f"в ленте {len(items)}, новых {cnt}")
            except Exception as e:
                log.warning("pr feed %s: %s", src["url"], e)
                store.set_source(src["id"], last_ts=time.time(), last_ok=0, last_note=f"ошибка: {e}"[:200])
                feed_err.append(f"{src['title']}: {e}")

        # оценки по старой рубрике (без design_focus) — переоценить
        store.db().execute("UPDATE articles SET stage='new' WHERE stage='scored' AND ai_json NOT LIKE '%design_focus%'")
        store.db().execute("UPDATE articles SET stage='new', reason='' WHERE stage='rejected' AND found>? AND reason IN "
                           "('блог компании','блог компании (Claude)','школа (Claude)')", (time.time() - MAX_AGE_DAYS * 86400,))
        store.db().commit()
        rated = rejected = errors = 0
        budget_stop = False
        for row in store.by_stage("new", PER_RUN):
            if score.over_budget():
                budget_stop = True
                break
            try:
                st = await _rate_one(s, row, _kind(row, kinds), use_ai=rated < AI_PER_RUN)
            except Exception as e:
                log.warning("pr rate %s: %s", row["url"], e)
                errors += 1
                continue
            rated += st == "scored"
            rejected += st == "rejected"
            errors += st == "error"
    note = (f"новых {new_total}, оценено {rated}, отсеяно {rejected}"
            + (f", не прочиталось {errors}" if errors else "") + f", {time.time() - t0:.0f} с")
    if feed_err:
        note += f"; источники с ошибкой: {len(feed_err)}"
        await say("⚠️ PR: не читаются источники:\n" + "\n".join(f"∙ {e}" for e in feed_err[:10])
                  + "\n\n/pr_test покажет подробности.")
    if budget_stop:
        await say(f"⚠️ PR: месячный лимит API (${config.MONTHLY_LIMIT_USD:.0f}) исчерпан — оценку остановил.")
    return note


# ---------- подготовка карточки: контакты, сверка с дизайнерс, черновик ----------

def _ext_links(r: dict, cs: list[dict]) -> list[str]:
    out = [r.get("author_url") or ""]
    for c in cs:
        if c["confidence"] != "high":
            continue
        out.append({"tg": f"t.me/{c['value']}", "vk": f"vk.com/{c['value']}"}.get(c["channel"], c["value"]))
    return [x for x in out if x]


async def prepare(r: dict, draft: bool = True) -> dict | None:
    """Карточка готова к показу → обновлённая статья; None — статья уже есть на дизайнерс (отсеяна)."""
    try:
        cs = await contacts.find(r, r.get("links") or [])
    except Exception:
        log.exception("contacts %s", r["url"])
        cs = store.contacts_for(r["id"])
    m, slug = authors.match(r, _ext_links(r, cs))
    store.update_article(r["id"], match=m, dsg_slug=slug)
    if m == "sure" and authors.has_article(slug, r["title"]):
        store.update_article(r["id"], stage="rejected", reason="уже на дизайнерс")
        return None
    if draft and outreach_on() and not store.outreach_for_article(r["id"]):
        cs = await contacts.validate(cs) if cs else cs
        au = r.get("author_url", "")
        best = contacts.best(cs, skip=lambda c: store.stopped(c["value"], au) or store.already_contacted(c["value"], au))
        if best:
            d = await outreach.draft(store.article(r["id"]), m)
            if d.get("text"):
                store.add_outreach(r["id"], best["id"], best["channel"], best["value"], d["text"])
    return store.article(r["id"])


def _ago(ts: float) -> str:
    if not ts:
        return "дата неизвестна"
    h = (time.time() - ts) / 3600
    if h < 24:
        return f"{max(1, int(h))} ч назад"
    return f"{int(h // 24)} дн. назад"


def _k(n: float) -> str:
    n = int(n or 0)
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


ST = {"ok": "✅", "dead": "❌ нет такого", "bot": "🤖 бот", "channel": "📢 канал", "unchecked": "· не проверен",
      "unknown": "· не проверен", "manual": "✋ вручную"}
OUT_ST = {"draft": "черновик", "approved": "⏳ в очереди на отправку", "sent": "✉️ отправлено", "read": "👀 прочитано",
          "replied": "💬 ответил", "skipped": "⏭ пропущено", "failed": "⚠️ не отправилось", "stopped": "🚫 стоп-лист"}


def _contact_line(c: dict) -> str:
    v = f"@{c['value']}" if c["channel"] == "tg" else c["value"] if c["channel"] == "email" else f"vk.com/{c['value']}"
    src = "ссылка в статье" if c["source"].startswith("ссылка в статье") else c["source"]
    return f"∙ {v} — {ST.get(c['status'], c['status'])}{' (' + c['note'] + ')' if c['note'] and c['status'] == 'ok' else ''}" \
           f" · {'уверенно' if c['confidence'] == 'high' else 'не точно'} · {src}"


def card(r: dict) -> str:
    ai = r.get("ai_json") or {}
    st = r.get("stats") or {}
    company = r.get("track") == "company"
    tag = "🏢 Компания" if company else ""
    tag = tag or {"none": "🆕 Новый автор", "sure": f"💤 Есть на дизайнерс: dsgners.ru/{r.get('dsg_slug')}",
           "maybe": f"❔ Возможно, есть на дизайнерс: dsgners.ru/{r.get('dsg_slug')}"}.get(r.get("match") or "", "")
    lines = [f"⭐ {r['total']} · {SITE_NAME.get(r['site'], r['site'])} · {_ago(r['published'])}"
             + (f" · {ai['type']}" if ai.get("type") else "")]
    if tag:
        lines.append(tag)
    lines += [r["title"], r["url"]]
    if r.get("author"):
        lines.append(f"Автор: {r['author']}" + (f" — {r['author_url']}" if r.get("author_url") else ""))
    au = r.get("author_stats") or {}
    prof = " · ".join(x for x in (f"{int(au['posts'])} публ." if au.get("posts") else "",
                                  f"карма {int(au['karma'])}" if au.get("karma") else "",
                                  f"рейтинг {int(au['rating'])}" if au.get("rating") else "",
                                  f"{_k(au['followers'])} подписч." if au.get("followers") else "") if x)
    hist = store.author_history(r.get("author_url") or "", MIN_SCORE, r["id"])
    if hist[0]:
        prof += (" · " if prof else "") + f"у нас в базе ещё {hist[0]}, сильных {hist[1]}"
    if prof or r.get("author_score") is not None:
        lines.append(f"Профиль: {prof or 'нет данных'}"
                     + (f" → {r['author_score']:.1f}/10" if r.get("author_score") is not None else ""))
    if ai.get("why"):
        lines.append(f"Почему: {ai['why']}")
    nums = " ".join(x for x in (f"🔖{_k(st.get('favorites'))}" if st.get("favorites") else "",
                                f"💬{_k(st.get('comments'))}" if st.get("comments") else "",
                                f"👁{_k(st.get('views'))}" if st.get("views") else "") if x)
    lines.append(f"Claude {r['ai']:.0f}/10 · площадка {r['signal']:.1f}/10"
                 + (f" · автор {r['author_score']:.1f}/10" if r.get("author_score") is not None else "")
                 + (f" · {nums}" if nums else ""))
    cs = store.contacts_for(r["id"])
    if cs:
        lines.append("\nКонтакты:")
        lines += [_contact_line(c) for c in cs[:5]]
    else:
        lines.append("\nКонтактов автор не оставил.")
    outs = store.outreach_for_article(r["id"])
    o = next((x for x in reversed(outs) if x["kind"] == "first"), None)
    if o:
        to = {"tg": f"@{o['value']}", "vk": f"vk.com/{o['value']}"}.get(o["channel"], o["value"])
        lines.append(f"\n✉️ {OUT_ST.get(o['status'], o['status'])} → {to}"
                     + (f" ({o['note']})" if o["note"] and o["status"] in ("failed", "skipped") else ""))
        lines.append(o["text"])
        if o["channel"] == "vk":
            lines.append("\n(VK — отправь вручную и нажми «📋 Отправил вручную»)")
    elif cs and outreach_on():
        lines.append("\nЧерновика нет: нет проверенного контакта" + ("" if sender.ready() else
                     " — аккаунт менеджера не подключён, @ не проверены (/pr_login)"))
    return "\n".join(lines)


def followup_card(o: dict) -> str:
    a = store.article(o["article_id"]) or {}
    return (f"🔔 Напоминание @{o['value']} — прочитал первое сообщение и не ответил\n"
            f"Статья: «{a.get('title', '')}»\n{a.get('url', '')}\n\n"
            f"✉️ {OUT_ST.get(o['status'], o['status'])}:\n{o['text']}")


async def digest() -> str:
    since = time.time() - 86400
    c = store.counts_since(since)
    reasons = ", ".join(f"{r} {n}" for r, n in store.reasons_since(since)[:5])
    up, down = store.votes_total()
    rows, comps = [], []
    for track, lim, acc in (("author", DIGEST_MAX, rows), ("company", DIGEST_COMPANIES, comps)):
        for r in store.digest_candidates(MIN_SCORE, lim * 2, track):
            if len(acc) >= lim:
                break
            p = await prepare(r)
            if p:
                acc.append(p)
    head = (f"🤝 PR · дайджест на {_now():%d.%m}\n"
            f"За сутки найдено {c['found']}, отсеяно {c['rejected']}" + (f" ({reasons})" if reasons else "")
            + f", оценено {c['scored'] + c['sent']}. В подборке: авторов {len(rows)}, компаний {len(comps)}.")
    if outreach_on():
        head += f"\nОтправка: {sender_line()}"
    if up + down < 30:
        head += ("\n\nИдёт калибровка: отметь каждую карточку 👍 или 👎 — по отметкам подстрою оценку. "
                 f"Отмечено пока {up + down}.")
    if not rows and not comps:
        best = store.best_below(MIN_SCORE)
        head += f"\n\nВыше порога {MIN_SCORE:g} ничего нет."
        if best:
            best = await prepare(best, draft=False)
        if best:
            head += f" Лучшая из оценённых — {best['total']}:"
        await say(head)
        if best:
            await say(card(best), best["id"])
            store.update_article(best["id"], stage="sent", sent=time.time())
        return "пусто" + (", показал лучшую ниже порога" if best else "")
    await say(head)
    for title, group in (("👤 Авторы", rows), ("🏢 Компании", comps)):
        if not group:
            continue
        await say(f"{title} — {len(group)}")
        for r in group:
            await say(card(r), r["id"])
            store.update_article(r["id"], stage="sent", sent=time.time())
            await asyncio.sleep(0.4)
    drafts = sum(1 for r in rows + comps if store.outreach_for_article(r["id"]))
    return f"авторов {len(rows)}, компаний {len(comps)}, с черновиком {drafts}"


def sender_line() -> str:
    tg = (f"Telegram {sender.me()}" if sender.ready() else "Telegram не подключён (/pr_login)")
    mail = (f"почта {sender.SMTP['USER']}" if sender.mail_ready() else "почта не настроена")
    p = sender.paused()
    return (f"{tg} · {mail} · сегодня {sender.sent_today()}/{sender.limit_today()} · окно "
            f"{sender.WINDOW[0]}:00–{sender.WINDOW[1]}:00" + (f" · ⏸ пауза: {p}" if p else ""))


async def one(url: str) -> str:
    """/pr_author адрес — разобрать одну статью и прислать карточку."""
    url = sources._clean_url(url.strip())
    site = "habr" if "habr.com" in url else "vc" if "vc.ru" in url else "rss"
    if not store.known(url):
        store.add_article(url, site, 0, "", "", 0)
    row = store.db().execute("SELECT id FROM articles WHERE url=?", (url,)).fetchone()
    r = store.article(row[0])
    if r["stage"] in ("new", "error", "rejected"):
        store.update_article(r["id"], stage="new", reason="")
        async with sources.session() as s:
            st = await _rate_one(s, store.article(r["id"]), site)
        r = store.article(r["id"])
        if st != "scored":
            return f"Статья отсеяна: {r['reason'] or st}."
    p = await prepare(r)
    if not p:
        return f"Эта статья уже есть у автора на дизайнерс (dsgners.ru/{store.article(r['id'])['dsg_slug']})."
    await say(card(p), p["id"])
    return "карточка выше"


def vote(aid: int, v: int) -> dict | None:
    r = store.article(aid)
    if r:
        store.update_article(aid, vote=v, vote_ts=time.time())
    return r


# ---------- действия с черновиком (кнопки) ----------

def approve(oid: int) -> dict | None:
    o = store.outreach(oid)
    if o and o["status"] in ("draft", "failed"):
        store.set_outreach(oid, status="approved", approved=time.time(), note="")
    return store.outreach(oid)


async def redraft(oid: int, comment: str = "") -> dict | None:
    o = store.outreach(oid)
    if not o or o["status"] not in ("draft", "failed"):
        return o
    a = store.article(o["article_id"])
    if o["kind"] == "followup":
        first = next((x for x in store.outreach_for_article(a["id"]) if x["kind"] == "first"), None)
        text = await outreach.follow_up((first or {}).get("text", ""), a)
    else:
        if comment:
            outreach.add_rule(comment)
        text = (await outreach.draft(a, a.get("match") or "none", comment=comment, previous=o["text"])).get("text")
    if text:
        store.set_outreach(oid, text=text, status="draft")
    return store.outreach(oid)


def skip(oid: int):
    store.set_outreach(oid, status="skipped")


def stop_forever(oid: int):
    o = store.outreach(oid)
    if not o:
        return
    a = store.article(o["article_id"]) or {}
    store.stop(o["value"], "владелец: не писать")
    if a.get("author_url"):
        store.stop(a["author_url"], "владелец: не писать")
    for c in store.contacts_for(o["article_id"]):
        store.stop(c["value"], "владелец: не писать")
    store.set_outreach(oid, status="stopped")


def manual_sent(oid: int):
    store.set_outreach(oid, status="sent", sent=time.time(), note="вручную")


# ---------- ежедневное: напоминания, @SpamBot, чистка ----------

async def daily() -> str:
    notes = []
    if sender.ready():
        due = await sender.check_read()
        for o in due:
            a = store.article(o["article_id"])
            text = await outreach.follow_up(o["text"], a)
            if text:
                oid = store.add_outreach(o["article_id"], o["contact_id"], o["channel"], o["value"], text, kind="followup")
                await say(followup_card(store.outreach(oid)), None, oid)
        notes.append(f"напоминаний {len(due)}")
        notes.append(f"@SpamBot: {await sender.spambot()}")
    n = store.forget_contacts(CONTACT_TTL)
    if n:
        notes.append(f"удалено старых контактов {n}")
    return ", ".join(notes) or "нечего делать"


async def weekly() -> str:
    since = time.time() - 7 * 86400
    c = store.counts_since(since)
    o = store.outreach_stats(since)
    up, down = store.votes_total()
    by_site = store.db().execute(
        "SELECT site, COUNT(*) n, ROUND(AVG(total),1) t FROM articles WHERE found>=? AND stage IN ('scored','sent') "
        "GROUP BY site ORDER BY n DESC", (since,)).fetchall()
    joined = store.db().execute(
        "SELECT COUNT(DISTINCT a.author_url) FROM outreach o JOIN articles a ON a.id=o.article_id "
        "WHERE o.status='replied' AND o.replied>=?", (since,)).fetchone()[0]
    text = (f"🤝 PR · итоги недели\n\n"
            f"Статей найдено {c['found']}, отсеяно {c['rejected']}, в дайджестах {c['sent']}\n"
            f"Площадки: " + (", ".join(f"{SITE_NAME.get(r['site'], r['site'])} {r['n']} (ср. {r['t']})" for r in by_site) or "—")
            + f"\nЧерновиков {o['drafts']}, отправлено {o['sent']}, прочитали {o['read']}, ответили {o['replied']}"
            f" ({joined} авторов)\nОтметки всего: 👍 {up} · 👎 {down}\n"
            f"Правил для сообщений из твоих правок: {len(outreach.rules())}\n"
            f"Потрачено на PR в этом месяце: ${spend.by_agent_this_month().get('pr', 0):.2f}")
    await say(text)
    return "отправлено"


# ---------- проверка источников и статус ----------

async def test() -> str:
    """Каждый источник: читается ли лента и что достаётся из первой статьи. Без Claude и без записи в базу."""
    out = []
    async with sources.session() as s:
        for src in store.sources():
            flag = "" if src["enabled"] else " (выключен)"
            try:
                items = await sources.read_feed(s, src["url"])
            except Exception as e:
                out.append(f"❌ #{src['id']} {src['title']}{flag}\n   лента: {e}")
                continue
            if not items:
                out.append(f"⚠️ #{src['id']} {src['title']}{flag}\n   лента пустая")
                continue
            it = items[0]
            try:
                a = await sources.details(s, src["kind"], it)
                st = ", ".join(f"{k} {int(v)}" for k, v in a.stats.items() if v) or "нет"
                au = ", ".join(f"{k} {int(v)}" for k, v in a.author_stats.items() if v) or "нет"
                out.append(f"✅ #{src['id']} {src['title']}{flag}: в ленте {len(items)}\n"
                           f"   «{a.title[:70]}» — через {a.via or '?'}\n"
                           f"   автор: {a.author or '—'} {a.author_url}\n"
                           f"   знаков {len(a.text)}, картинок {a.images}, ссылок в тексте {len(a.links)}, блог компании: "
                           f"{'да' if a.corporate else 'нет' if a.corporate is False else 'не знаю'}, "
                           f"перевод: {'да' if a.translation else 'нет'}\n"
                           f"   цифры: {st}; автор: {au}\n"
                           f"   фильтр: {score.filter_reason(a, src['kind']) or 'прошла'}, площадка {score.signal(a)}/10")
            except Exception as e:
                out.append(f"⚠️ #{src['id']} {src['title']}{flag}: в ленте {len(items)}, но статья не прочиталась: {e}")
    out.append(f"Аккаунт отправки: {sender_line()}")
    out.append(f"Авторов дизайнерс в базе сверки: {len(store.dsg_authors())}")
    return "🧪 Проверка PR\n\n" + "\n\n".join(out)


def status() -> str:
    runs = store.runs()

    def last(n):
        r = runs.get(n)
        if not r:
            return "ещё не было"
        return f"{dt.datetime.fromtimestamp(r['ts'], ZoneInfo(config.SEO_TZ)):%d.%m %H:%M} — {r['note']}"
    up, down = store.votes_total()
    c = store.counts_since(time.time() - 7 * 86400)
    o = store.outreach_stats(time.time() - 7 * 86400)
    queue = len(store.outreach_by("approved"))
    sb = store.get("spambot") or {}
    lines = [f"🤝 PR-агент: {'включён' if ENABLED else 'выключен (PR_ENABLED=0)'}",
             f"Сбор {SCAN_HOUR:02d}:00, дайджест {DIGEST_HOUR:02d}:00 (МСК). Порог {MIN_SCORE:g}, до {DIGEST_MAX} карточек.",
             f"Модели: оценка {score.MODEL}, сообщения {outreach.MODEL}. "
             f"Потрачено в этом месяце: ${spend.by_agent_this_month().get('pr', 0):.2f}",
             f"Последний сбор: {last('scan')}", f"Последний дайджест: {last('digest')}",
             f"Напоминания и проверки: {last('daily')}", f"Профили дизайнерс: {last('authors')}",
             f"За 7 дней: найдено {c['found']}, отсеяно {c['rejected']}, в дайджесте {c['sent']}, ждут {c['new']}",
             f"Отметки: 👍 {up} · 👎 {down}", "",
             f"Черновики: {'включены' if outreach_on() else 'выключены'} (/pr_outreach on|off)",
             f"Отправка: {sender_line()}", f"В очереди: {queue}",
             f"За 7 дней: отправлено {o['sent']}, прочитали {o['read']}, ответили {o['replied']}",
             f"@SpamBot: {'ок' if sb.get('ok') else 'ограничен' if sb else 'не проверялся'}",
             f"Правил для сообщений: {len(outreach.rules())}", "", "Источники:"]
    for s in store.sources():
        mark = "⏸" if not s["enabled"] else "✅" if s["last_ok"] else "❌" if s["last_ok"] == 0 else "·"
        lines.append(f"{mark} #{s['id']} {s['title']} — {s['last_note'] or 'ещё не читался'}")
    return "\n".join(lines)


# ---------- расписание ----------

def due(name: str) -> bool:
    now = _now()
    last = store.last_run(name)
    done_today = bool(last) and _date(last) >= now.date()
    if name == "authors":   # раз в неделю (и сразу, если база пустая)
        return not store.dsg_authors() and not last or (now.weekday() == 0 and now.hour >= 6 and not done_today)
    if name == "scan":
        return now.hour >= SCAN_HOUR and not done_today
    if name == "digest":
        scan_last = store.last_run("scan")
        scanned_today = bool(scan_last) and _date(scan_last) >= now.date()
        return now.hour >= DIGEST_HOUR and not done_today and (scanned_today or now.hour >= DIGEST_HOUR + 3)
    if name == "daily":
        return now.hour >= 11 and not done_today
    if name == "weekly":
        return now.weekday() == 0 and (now.hour, now.minute) >= (10, 30) and not done_today
    return False


JOBS = {"authors": authors.refresh, "scan": scan, "digest": digest, "daily": daily, "weekly": weekly}


async def run(name: str) -> str:
    if name in _running:
        return "уже выполняется"
    _running.add(name)
    try:
        note = await JOBS[name]()
        store.mark_run(name, True, note)
        log.info("pr %s: %s", name, note)
        return note
    except Exception as e:
        log.exception("pr %s failed", name)
        store.mark_run(name, False, f"ошибка: {e}")
        await say(f"⚠️ PR ({name}) упал: {e}")
        return f"ошибка: {e}"
    finally:
        _running.discard(name)


async def loop():
    if not ENABLED:
        log.info("PR-агент выключен (PR_ENABLED=0)")
        return
    store.db()
    await asyncio.sleep(40)
    try:
        await sender.start()
    except Exception:
        log.exception("PR: отправка не запустилась")
    while True:
        if not _running:                         # по одной задаче за раз: сбор → дайджест → остальное
            for name in ("authors", "scan", "digest", "daily", "weekly"):
                try:
                    if due(name):
                        asyncio.create_task(run(name))
                        break
                except Exception:
                    log.exception("pr scheduler")
        await asyncio.sleep(60)
