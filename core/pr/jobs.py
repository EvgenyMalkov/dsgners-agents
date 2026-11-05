"""Работа PR-агента: сбор, дайджест, проверка источников, статус и расписание (время — SEO_TZ, Москва).

07:00 — сбор новых статей за сутки и оценка (тихо)
10:00 — дайджест: до PR_DIGEST_MAX карточек с итоговой оценкой ≥ PR_MIN_SCORE, кнопки 👍/👎
"""
import asyncio
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

from core import config, spend
from core.pr import score, sources, store

log = logging.getLogger("pr.jobs")

ENABLED = config.env("PR_ENABLED", "1") == "1"
MIN_SCORE = float(config.env("PR_MIN_SCORE", "7"))
DIGEST_MAX = int(config.env("PR_DIGEST_MAX", "10"))
MAX_AGE_DAYS = float(config.env("PR_MAX_AGE_DAYS", "7"))
PER_RUN = int(config.env("PR_PER_RUN", "120"))          # статей разобрать за один сбор
AI_PER_RUN = int(config.env("PR_AI_PER_RUN", "60"))     # из них оценить Claude
SCAN_HOUR = int(config.env("PR_SCAN_HOUR", "7"))
DIGEST_HOUR = int(config.env("PR_DIGEST_HOUR", "10"))
SITE_NAME = {"habr": "Хабр", "vc": "vc.ru"}

_send = None      # async (text, article_id | None) — в Telegram владельцам; с id — с кнопками 👍/👎
_running: set[str] = set()


def setup(send):
    global _send
    _send = send


async def say(text: str, aid: int | None = None):
    if _send:
        try:
            await _send(text, aid)
        except Exception:
            log.exception("pr notify failed")


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

        rated = rejected = errors = 0
        budget_stop = False
        for row in store.by_stage("new", PER_RUN):
            kind = _kind(row, kinds)
            item = sources.Item(url=row["url"], title=row["title"], author=row["author"], published=row["published"] or 0)
            try:
                a = await sources.details(s, kind, item)
            except Exception as e:
                store.update_article(row["id"], stage="error", reason=f"не прочиталась: {e}"[:200])
                errors += 1
                continue
            reason = score.filter_reason(a, kind)
            sig = score.signal(a)
            store.update_article(row["id"], title=a.title or row["title"], author=a.author, author_url=a.author_url,
                                 published=a.published or row["published"], chars=len(a.text), images=a.images,
                                 stats=a.stats, author_stats=a.author_stats, signal=sig)
            if reason:
                store.update_article(row["id"], stage="rejected", reason=reason)
                rejected += 1
                continue
            if rated >= AI_PER_RUN:
                continue                       # останется «new» — оценим в следующий раз
            if score.over_budget():
                budget_stop = True
                break
            try:
                r = await score.ai_rate(a)
            except Exception as e:
                log.warning("pr ai %s: %s", a.url, e)
                errors += 1
                continue
            rated += 1
            if r.get("author_kind") in ("company", "school"):
                store.update_article(row["id"], stage="rejected", ai=r["score"], ai_json=r,
                                     reason="блог компании (Claude)" if r["author_kind"] == "company" else "школа (Claude)")
                rejected += 1
            elif not r.get("design", True):
                store.update_article(row["id"], stage="rejected", ai=r["score"], ai_json=r, reason="не про дизайн (Claude)")
                rejected += 1
            else:
                store.update_article(row["id"], stage="scored", ai=r["score"], ai_json=r, total=score.total(r["score"], sig))
    note = (f"новых {new_total}, оценено Claude {rated}, отсеяно {rejected}"
            + (f", не прочиталось {errors}" if errors else "") + f", {time.time() - t0:.0f} с")
    if feed_err:
        note += f"; источники с ошибкой: {len(feed_err)}"
        await say("⚠️ PR: не читаются источники:\n" + "\n".join(f"∙ {e}" for e in feed_err[:10])
                  + "\n\n/pr_test покажет подробности.")
    if budget_stop:
        await say(f"⚠️ PR: месячный лимит API (${config.MONTHLY_LIMIT_USD:.0f}) исчерпан — оценку остановил.")
    return note


# ---------- дайджест ----------

def _ago(ts: float) -> str:
    if not ts:
        return "дата неизвестна"
    h = (time.time() - ts) / 3600
    if h < 24:
        return f"{max(1, int(h))} ч назад"
    d = int(h // 24)
    return f"{d} дн. назад"


def _k(n: float) -> str:
    n = int(n or 0)
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def card(r: dict) -> str:
    ai = r.get("ai_json") or {}
    st = r.get("stats") or {}
    lines = [f"⭐ {r['total']} · {SITE_NAME.get(r['site'], r['site'])} · {_ago(r['published'])}"
             + (f" · {ai['type']}" if ai.get("type") else ""),
             r["title"], r["url"]]
    if r.get("author"):
        lines.append(f"Автор: {r['author']}" + (f" — {r['author_url']}" if r.get("author_url") else ""))
    if ai.get("why"):
        lines.append(f"Почему: {ai['why']}")
    nums = " ".join(x for x in (f"🔖{_k(st.get('favorites'))}" if st.get("favorites") else "",
                                f"💬{_k(st.get('comments'))}" if st.get("comments") else "",
                                f"👁{_k(st.get('views'))}" if st.get("views") else "") if x)
    lines.append(f"Claude {r['ai']:.0f}/10 · площадка {r['signal']:.1f}/10" + (f" · {nums}" if nums else ""))
    return "\n".join(lines)


async def digest() -> str:
    since = time.time() - 86400
    c = store.counts_since(since)
    rows = store.digest_candidates(MIN_SCORE, DIGEST_MAX)
    reasons = ", ".join(f"{r} {n}" for r, n in store.reasons_since(since)[:5])
    up, down = store.votes_total()
    head = (f"🤝 PR · дайджест на {_now():%d.%m}\n"
            f"За сутки найдено {c['found']}, отсеяно {c['rejected']}" + (f" ({reasons})" if reasons else "")
            + f", оценено {c['scored'] + c['sent']}. В подборке: {len(rows)}.")
    if up + down < 30:
        head += ("\n\nИдёт калибровка: отметь каждую карточку 👍 или 👎 — по отметкам подстрою оценку. "
                 f"Отмечено пока {up + down}.")
    if not rows:
        best = store.best_below(MIN_SCORE)
        head += f"\n\nВыше порога {MIN_SCORE:g} ничего нет."
        if best:
            head += f" Лучшая из оценённых — {best['total']}:"
        await say(head)
        if best:
            await say(card(best), best["id"])
            store.update_article(best["id"], stage="sent", sent=time.time())
        return "пусто" + (", показал лучшую ниже порога" if best else "")
    await say(head)
    for r in rows:
        await say(card(r), r["id"])
        store.update_article(r["id"], stage="sent", sent=time.time())
        await asyncio.sleep(0.4)
    return f"отправлено карточек: {len(rows)}"


def vote(aid: int, v: int) -> dict | None:
    r = store.article(aid)
    if r:
        store.update_article(aid, vote=v, vote_ts=time.time())
    return r


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
                           f"   знаков {len(a.text)}, картинок {a.images}, блог компании: "
                           f"{'да' if a.corporate else 'нет' if a.corporate is False else 'не знаю'}, "
                           f"перевод: {'да' if a.translation else 'нет'}\n"
                           f"   цифры: {st}; автор: {au}\n"
                           f"   фильтр: {score.filter_reason(a, src['kind']) or 'прошла'}, площадка {score.signal(a)}/10")
            except Exception as e:
                out.append(f"⚠️ #{src['id']} {src['title']}{flag}: в ленте {len(items)}, но статья не прочиталась: {e}")
    return "🧪 Проверка источников PR\n\n" + "\n\n".join(out)


def status() -> str:
    runs = store.runs()

    def last(n):
        r = runs.get(n)
        if not r:
            return "ещё не было"
        return f"{dt.datetime.fromtimestamp(r['ts'], ZoneInfo(config.SEO_TZ)):%d.%m %H:%M} — {r['note']}"
    up, down = store.votes_total()
    c = store.counts_since(time.time() - 7 * 86400)
    lines = [f"🤝 PR-агент: {'включён' if ENABLED else 'выключен (PR_ENABLED=0)'}",
             f"Сбор {SCAN_HOUR:02d}:00, дайджест {DIGEST_HOUR:02d}:00 (МСК). Порог {MIN_SCORE:g}, до {DIGEST_MAX} карточек.",
             f"Модель оценки: {score.MODEL}. Потрачено в этом месяце: ${spend.by_agent_this_month().get('pr', 0):.2f}",
             f"Последний сбор: {last('scan')}", f"Последний дайджест: {last('digest')}",
             f"За 7 дней: найдено {c['found']}, отсеяно {c['rejected']}, в дайджесте {c['sent']}, ждут {c['new']}",
             f"Отметки: 👍 {up} · 👎 {down}", "", "Источники:"]
    for s in store.sources():
        mark = "⏸" if not s["enabled"] else "✅" if s["last_ok"] else "❌" if s["last_ok"] == 0 else "·"
        lines.append(f"{mark} #{s['id']} {s['title']} — {s['last_note'] or 'ещё не читался'}")
    lines.append("\nКонтакты и отправка — следующие этапы, пока выключены.")
    return "\n".join(lines)


# ---------- расписание ----------

def due(name: str) -> bool:
    now = _now()
    last = store.last_run(name)
    done_today = bool(last) and _date(last) >= now.date()
    if name == "scan":
        return now.hour >= SCAN_HOUR and not done_today
    if name == "digest":
        scan_last = store.last_run("scan")
        scanned_today = bool(scan_last) and _date(scan_last) >= now.date()
        return now.hour >= DIGEST_HOUR and not done_today and (scanned_today or now.hour >= DIGEST_HOUR + 3) \
            and "scan" not in _running
    return False


async def run(name: str) -> str:
    if name in _running:
        return "уже выполняется"
    _running.add(name)
    try:
        note = await {"scan": scan, "digest": digest}[name]()
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


async def _bg(name: str):
    _running.discard(name)
    await run(name)


async def loop():
    if not ENABLED:
        log.info("PR-агент выключен (PR_ENABLED=0)")
        return
    store.db()
    await asyncio.sleep(40)
    while True:
        for name in ("scan", "digest"):
            try:
                if name not in _running and due(name):
                    _running.add(name)          # занять сразу, чтобы дайджест не стартовал раньше сбора
                    asyncio.create_task(_bg(name))
                    break
            except Exception:
                log.exception("pr scheduler")
        await asyncio.sleep(60)
