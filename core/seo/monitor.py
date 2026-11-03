"""Что SEO-мониторинг делает по расписанию.

hourly     — сайт отвечает, robots.txt не закрыл сайт и не поменялся молча, SSL не истекает
new_pages  — (каждые 30 минут) новые и изменённые адреса в sitemap: проверка, переобход в Яндексе,
             IndexNow, и для новых статей — задача SEO-агенту «оформить» (title, description, разметка, alt, ссылки)
daily      — Вебмастер (страницы в поиске, критичные проблемы) и Метрика (поисковый трафик вчера к прошлой неделе)
weekly     — полный обход сайта, отчёт с проблемами, запросы на 4–15 местах, просевшие страницы,
             и задача SEO-агенту превратить отчёт в список задач по приоритету

В Telegram уходят только алерты (что-то сломалось/починилось) и недельный отчёт — без «всё ок» каждый день.
"""
import asyncio
import datetime as dt
import logging
import re
import socket
import ssl
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from core import config
from core.seo import apis, page, store

log = logging.getLogger("seo.monitor")

_notify = None        # async (text, files: list[Path] | None) — в Telegram владельцам
_start_job = None     # (agent, task, source, full_task) — запуск задачи агенту


def setup(notify, start_job):
    global _notify, _start_job
    _notify, _start_job = notify, start_job


async def say(text: str, files: list[Path] | None = None):
    from core.office import office
    office.emit("seo", "info", text.split("\n", 1)[0][:200])
    if _notify:
        try:
            await _notify(text, files or [])
        except Exception:
            log.exception("notify failed")


async def alert(key: str, active: bool, text: str, fixed_text: str = ""):
    """Сообщить о проблеме один раз, когда появилась, и один раз — когда ушла."""
    st = store.alert_state(key, active, text)
    if st == "new":
        await say("🚨 SEO: " + text)
    elif st == "resolved":
        await say("✅ SEO: " + (fixed_text or f"починилось — {text}"))


def site_url(path="/") -> str:
    return f"https://{config.SEO_SITE}{path}"


def ignored(url: str) -> bool:
    return bool(config.SEO_IGNORE_PATTERN and re.search(config.SEO_IGNORE_PATTERN, url))


def is_content(url: str) -> bool:
    """Статья, новость, список — то, что стоит оформлять под поиск."""
    if ignored(url):
        return False
    if config.SEO_CONTENT_PATTERN:
        return bool(re.search(config.SEO_CONTENT_PATTERN, url))
    return urlparse(url).path.strip("/") != ""


def _session():
    return aiohttp.ClientSession(headers={"User-Agent": page.UA}, timeout=aiohttp.ClientTimeout(total=30))


# ---------------- каждый час ----------------

def ssl_days_left(host: str) -> int:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as s:
            exp = ssl.cert_time_to_seconds(s.getpeercert()["notAfter"])
    return int((exp - time.time()) // 86400)


async def hourly() -> str:
    async with _session() as s:
        home = await page.fetch(site_url("/"), s)
        for i in range(2):     # разовый сбой — не повод будить владельца
            if home.status == 200:
                break
            await asyncio.sleep(page.RECHECK_PAUSE * (i + 1))
            home = await page.fetch(site_url("/"), s, timeout=60)
        rob = await page.fetch(site_url("/robots.txt"), s)
    up = home.status == 200
    await alert("down", not up,
                f"сайт {config.SEO_SITE} не отвечает нормально: "
                f"{'HTTP ' + str(home.status) if home.status else home.error}",
                f"сайт {config.SEO_SITE} снова отвечает (HTTP 200, {home.ms} мс)")
    if up:
        await alert("slow_home", home.ms > 5000, f"главная отвечает медленно: {home.ms} мс",
                     f"главная снова отвечает быстро: {home.ms} мс")

    note = f"главная HTTP {home.status}, {home.ms} мс"
    if rob.status == 200:
        await alert("robots_missing", False, "", "robots.txt снова доступен")
        blocked = page.robots_blocks_all(rob.html)
        await alert("robots_block", blocked,
                    "robots.txt ЗАКРЫЛ ВЕСЬ САЙТ от поисковиков (Disallow: /). Если это не специально — срочно вернуть.",
                    "robots.txt больше не закрывает сайт")
        prev = store.get("robots_txt")
        if prev is not None and prev.strip() != rob.html.strip():
            import difflib
            diff = "\n".join(l for l in difflib.unified_diff(prev.splitlines(), rob.html.splitlines(), lineterm="", n=0)
                             if not l.startswith(("---", "+++", "@@")))
            await say(f"ℹ️ SEO: изменился robots.txt\n\n{diff[:3000]}")
        store.put("robots_txt", rob.html)
    elif up and rob.status:
        await alert("robots_missing", rob.status >= 400, f"robots.txt отдаёт HTTP {rob.status}",
                    "robots.txt снова доступен")

    try:
        days = await asyncio.to_thread(ssl_days_left, config.SEO_SITE)
        await alert("ssl", days < 14, f"SSL-сертификат истекает через {days} дн. — продлить",
                    "SSL-сертификат продлён")
        note += f", SSL {days} дн."
    except Exception as e:
        note += f", SSL не проверен: {e}"
    return note


# ---------------- каждые 30 минут: новые страницы ----------------

async def read_sitemap() -> page.SitemapResult:
    robots = store.get("robots_txt") or ""
    maps = page.sitemaps_from_robots(robots) or [site_url("/sitemap.xml")]
    async with _session() as s:
        res = await page.read_sitemaps(maps, s)
    res.urls = {u: v for u, v in res.urls.items() if not ignored(u)}
    return res


async def sitemap_urls() -> dict[str, str]:
    """Все адреса сайта для обхода: из sitemap, а для не загрузившихся секций — последние известные."""
    res = await read_sitemap()
    out = {u: lm for u, (lm, _) in res.urls.items()}
    for u, lm in store.urls_from_files([f for f, _ in res.failed]).items():
        out.setdefault(u, lm)
    return out


async def _sitemap_failures(failed: list[tuple[str, str]]) -> str:
    """Секция sitemap не загрузилась: тревога, только если так 3 проверки подряд (~1,5 часа)."""
    streak = store.get("sitemap_fail_streak", {})
    now_failed = {f: why for f, why in failed}
    streak = {f: streak.get(f, 0) + 1 for f in now_failed}
    store.put("sitemap_fail_streak", streak)
    for f, n in streak.items():
        if n >= 3:
            await alert(f"sitemap_part:{f}", True, f"sitemap {f} не загружается уже {n} проверки подряд "
                        f"({now_failed[f]}). Поисковики тоже могут его не дочитать — проверить скорость генерации.")
    for a in store.active_alerts():
        if a["key"].startswith("sitemap_part:") and a["key"].split(":", 1)[1] not in now_failed:
            await alert(a["key"], False, "", f"sitemap {a['key'].split(':', 1)[1]} снова загружается")
    return (f"не загрузилось секций sitemap: {len(failed)} (повторю через 30 мин)") if failed else ""


def _optimized_today() -> int:
    day = dt.date.today().isoformat()
    return int(store.get(f"optimized:{day}", 0))


def start_optimize(url: str, rep: page.PageReport, auto: bool) -> str | None:
    """Поставить SEO-агенту задачу оформить статью."""
    if not _start_job:
        return None
    task = f"Оформить для поиска: {url}"
    full = (f"{task}\n\nИспользуй навык optimize-article.\n"
            f"{'Статья только что вышла — найдена мониторингом.' if auto else ''}\n\n"
            f"# Что уже есть на странице (автопроверка)\n{rep.summary()}")
    jid = _start_job("seo", task, "monitor" if auto else "telegram", full)
    store.mark_optimized(url)
    if auto:
        day = dt.date.today().isoformat()
        store.put(f"optimized:{day}", _optimized_today() + 1)
    return jid


async def new_pages() -> str:
    res = await read_sitemap()
    fail_note = await _sitemap_failures(res.failed)
    if not res.files:
        # не загрузилось вообще ничего — это сбой, а не «все страницы пропали»; ничего не трогаем
        await alert("sitemap", max(store.get("sitemap_fail_streak", {}).values() or [0]) >= 3,
                    "sitemap не загружается уже 1,5 часа — поисковики хуже находят новые страницы",
                    "sitemap снова читается")
        return "sitemap не загрузился, повторю через 30 мин"
    await alert("sitemap", False, "", "sitemap снова читается")
    urls = res.urls
    first = not store.known_urls()
    new, changed, gone = store.upsert_sitemap(urls, [f for f, _ in res.failed])
    if first:
        return f"первое знакомство с сайтом: в sitemap {len(urls)} адресов, запомнил. {fail_note}".strip()
    if len(gone) > max(20, len(urls) * 0.2):
        await say(f"⚠️ SEO: из sitemap пропало {len(gone)} адресов — их нет уже два успешных прочтения подряд "
                  f"(например: {', '.join(gone[:3])}). Если это не чистка — проверить генерацию sitemap.")

    todo = new[:50] + [u for u in changed if u not in new][:30]
    if not todo:
        return f"в sitemap {len(urls)} адресов, новых нет. {fail_note}".strip()

    reports: list[page.PageReport] = []
    async with _session() as s:
        for u in todo:
            reports.append(await page.check_url(u, s, is_article=is_content(u)))
            store.save_check(reports[-1])
            await asyncio.sleep(1 / max(config.SEO_CRAWL_RPS, 0.1))

    ok_new = [r.url for r in reports if r.url in new and r.status == 200 and not r.noindex]
    notes = []
    if ok_new and apis.has_yandex():
        try:
            rc = await apis.wm_recrawl(ok_new)
            notes.append(f"на переобход в Яндекс: {rc.get('sent', 0)}")
        except Exception as e:
            notes.append(f"переобход не отправлен: {e}")
    if ok_new and config.INDEXNOW_KEY:
        try:
            notes.append(f"IndexNow: {await apis.indexnow(ok_new)}")
        except Exception as e:
            notes.append(f"IndexNow: ошибка {e}")

    bad = [r for r in reports if any(l == "critical" for l, _, _ in r.issues)]
    if bad:
        lines = []
        for r in bad[:10]:
            crit = "; ".join(t for l, _, t in r.issues if l == "critical")
            lines.append(f"— {r.url}\n  {crit}")
        await say("🚨 SEO: проблемы на новых/обновлённых страницах\n\n" + "\n".join(lines))

    started = 0
    for r in reports:
        if (r.url in new and is_content(r.url) and r.status == 200 and not r.noindex
                and not store.was_optimized(r.url) and _optimized_today() < config.SEO_AUTO_OPTIMIZE_PER_DAY):
            if start_optimize(r.url, r, auto=True):
                started += 1
    if started:
        notes.append(f"на оформление SEO-агенту: {started}")
    return f"новых {len(new)}, изменённых {len(changed)}; " + "; ".join(notes + ([fail_note] if fail_note else []))


# ---------------- каждый день ----------------

async def daily() -> str:
    today = dt.date.today()
    notes = []
    if not apis.has_yandex():
        return "токен Яндекса не задан — Вебмастер и Метрика пропущены"
    try:
        sm = await apis.wm_summary()
        indexed = sm.get("searchable_pages_count", 0)
        excluded = sm.get("excluded_pages_count", 0)
        store.put_metric(today.isoformat(), "wm_indexed", indexed)
        notes.append(f"в поиске {indexed}, исключено {excluded}, ИКС {sm.get('sqi', '?')}")
        week_ago = store.metric((today - dt.timedelta(days=7)).isoformat(), "wm_indexed")
        if week_ago and indexed < week_ago * 0.9:
            await alert("indexed_drop", True, f"страниц в поиске Яндекса стало {indexed} (неделю назад {int(week_ago)}, "
                        f"−{100 - indexed * 100 // int(week_ago)}%) — смотреть «Исключённые страницы» в Вебмастере")
        else:
            await alert("indexed_drop", False, "", f"число страниц в поиске восстановилось: {indexed}")
        diags = await apis.wm_diagnostics() or []
        fatal = [d["code"] for d in diags if d["severity"] in ("FATAL", "CRITICAL")]
        await alert("wm_critical", bool(fatal), "Вебмастер видит критичные проблемы: " + ", ".join(fatal),
                    "критичных проблем в Вебмастере больше нет")
    except Exception as e:
        notes.append(f"Вебмастер: ошибка {e}")
    try:
        y = today - dt.timedelta(days=1)
        cur = await apis.organic_total(y, y)
        prev = await apis.organic_total(y - dt.timedelta(days=7), y - dt.timedelta(days=7))
        if cur is not None:
            store.put_metric(y.isoformat(), "organic", cur)
            notes.append(f"поиск вчера {cur} визитов (неделю назад {prev})")
            drop = bool(prev and prev >= 50 and cur < prev * 0.7)
            await alert("organic_drop", drop, f"поисковый трафик вчера {cur} визитов — на "
                        f"{100 - cur * 100 // max(prev or 1, 1)}% меньше, чем неделю назад ({prev})",
                        f"поисковый трафик вернулся: {cur} визитов вчера")
    except Exception as e:
        notes.append(f"Метрика: ошибка {e}")
    return "; ".join(notes)


# ---------------- раз в неделю: полный обход ----------------

async def crawl(urls: list[str]) -> list[page.PageReport]:
    sem = asyncio.Semaphore(2)
    delay = 2 / max(config.SEO_CRAWL_RPS, 0.1)   # два потока → в сумме SEO_CRAWL_RPS
    out: list[page.PageReport] = []
    async with _session() as s:
        async def one(u):
            async with sem:
                rep = await page.check_url(u, s, is_article=is_content(u), tries=1)
                out.append(rep)
                await asyncio.sleep(delay)
        await asyncio.gather(*(one(u) for u in urls))
        # то, что не открылось, перепроверяем в конце по одной, медленно, с паузами
        failed = [i for i, r in enumerate(out) if page.is_transient(r)]
        if failed:
            await asyncio.sleep(60)
        for i in failed:
            out[i] = await page.check_url(out[i].url, s, is_article=is_content(out[i].url), tries=3)
            await asyncio.sleep(3)
    for r in out:
        store.save_check(r)
    return out


def _dupes(reports, attr_name, min_len=1) -> list[tuple[str, list[str]]]:
    groups = defaultdict(list)
    for r in reports:
        v = getattr(r, attr_name).strip().lower()
        if r.status == 200 and not r.noindex and len(v) >= min_len:
            groups[v].append(r.url)
    return sorted(((k, v) for k, v in groups.items() if len(v) > 1), key=lambda x: -len(x[1]))


async def weekly() -> tuple[str, Path | None]:
    urls = list(await sitemap_urls())
    urls = urls[: config.SEO_CRAWL_MAX]
    t0 = time.time()
    reports = await crawl(urls)
    ok = [r for r in reports if r.status == 200]

    # битые внутренние ссылки: адреса, на которые ссылаются, но которых нет в sitemap — проверяем выборку
    linked = Counter(l for r in ok for l in r.internal_links)
    in_sitemap = set(urls)
    outside = [u for u, _ in linked.most_common() if u not in in_sitemap and not ignored(u)][:300]
    broken = []
    async with _session() as s:
        for u in outside:
            f = await page.fetch(u, s, max_bytes=1000)
            if f.status >= 400 or f.status == 0:
                src = [r.url for r in ok if u in r.internal_links][:3]
                broken.append((u, f.status, src))
            await asyncio.sleep(1 / max(config.SEO_CRAWL_RPS, 0.1))
    orphans = [r.url for r in ok if is_content(r.url) and linked.get(r.url, 0) == 0]

    by_code = defaultdict(list)
    for r in reports:
        for lvl, code, text in r.issues:
            by_code[(lvl, code)].append((r.url, text))

    today = dt.date.today()
    L = [f"# SEO-отчёт {config.SEO_SITE} — {today:%d.%m.%Y}", "",
         f"Проверено страниц: {len(reports)} за {int((time.time() - t0) // 60)} мин. "
         f"HTTP 200: {len(ok)}, ошибок 4xx/5xx: {sum(1 for r in reports if r.status >= 400 or r.status == 0)}.", ""]
    names = {"critical": "Критично", "warning": "Важно", "info": "Можно улучшить"}
    for lvl in ("critical", "warning", "info"):
        items = sorted(((c, v) for (l, c), v in by_code.items() if l == lvl), key=lambda x: -len(x[1]))
        if not items:
            continue
        L.append(f"## {names[lvl]}")
        for code, lst in items:
            if code in page.TRANSIENT or code == "4xx":
                L.append(f"- **{page.LABELS.get(code, code)}** — {len(lst)} стр. (перепроверено 3 раза с паузами):")
                L += [f"  - {u} — {t}" for u, t in lst[:10]]
            else:
                L.append(f"- **{page.LABELS.get(code, code)}** — {len(lst)} стр. Примеры: "
                         + ", ".join(u for u, _ in lst[:5]))
        L.append("")
    for title, name in (("Одинаковые title", "title"), ("Одинаковые description", "description")):
        d = _dupes(reports, name, 10)
        if d:
            L.append(f"## {title} ({len(d)} групп)")
            L += [f"- «{k[:90]}» — {len(v)} стр.: " + ", ".join(v[:4]) for k, v in d[:15]]
            L.append("")
    if broken:
        L.append(f"## Битые внутренние ссылки ({len(broken)})")
        L += [f"- {u} → HTTP {st or 'нет ответа'}; ссылаются: {', '.join(src)}" for u, st, src in broken[:30]]
        L.append("")
    if orphans:
        L.append(f"## Страницы-сироты: есть в sitemap, но ни одна проверенная страница на них не ссылается ({len(orphans)})")
        L += [f"- {u}" for u in orphans[:30]]
        L.append("")

    # Вебмастер: запросы, где мы рядом с топом
    try:
        qs = await apis.wm_queries(7)
        if qs:
            near = sorted((q for q in qs if 3.5 < q["pos"] <= 15 and q["shows"] >= 20), key=lambda q: -q["shows"])
            if near:
                L.append("## Запросы на 4–15 месте (поднять в топ-5 = попасть в нейроответ)")
                L += [f"- «{q['query']}» — позиция {q['pos']:.1f}, показов {q['shows']}, кликов {q['clicks']}"
                      for q in near[:30]]
                L.append("")
    except Exception as e:
        L.append(f"_Вебмастер недоступен: {e}_\n")

    # Метрика: просевшие страницы неделя к неделе
    try:
        end = today - dt.timedelta(days=1)
        cur = await apis.organic_by_page(end - dt.timedelta(days=6), end)
        prev = await apis.organic_by_page(end - dt.timedelta(days=13), end - dt.timedelta(days=7))
        if cur is not None and prev is not None:
            tot_c, tot_p = sum(cur.values()), sum(prev.values())
            L.append(f"## Поисковый трафик: {tot_c} визитов за неделю (прошлая неделя {tot_p})")
            drops = sorted(((u, p, cur.get(u, 0)) for u, p in prev.items() if p >= 30 and cur.get(u, 0) < p * 0.7),
                           key=lambda x: x[2] - x[1])
            if drops:
                L.append("Просели больше чем на 30%:")
                L += [f"- {u}: {p} → {c}" for u, p, c in drops[:20]]
            L.append("")
    except Exception as e:
        L.append(f"_Метрика недоступна: {e}_\n")

    if not apis.has_yandex():
        L.append("_Данные Вебмастера и Метрики появятся, когда в .env будет YANDEX_OAUTH_TOKEN и METRIKA_COUNTER_ID._")

    report = "\n".join(L)
    out = config.OUTBOX / "seo-weekly" / f"seo-report-{today:%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, "utf-8")
    crit = sum(len(v) for (l, _), v in by_code.items() if l == "critical")
    await say(f"📊 Недельный SEO-отчёт: проверено {len(reports)} стр., критичных проблем {crit}, "
              f"битых ссылок {len(broken)}. Список задач по приоритету пришлёт SEO-агент.", [out])
    if _start_job:
        _start_job("seo", f"Недельный SEO-отчёт {today:%d.%m}: задачи по приоритету", "monitor",
                   "Ниже недельный отчёт автоматической проверки сайта. Преврати его в список задач по приоритету "
                   "(польза для трафика / трудозатраты): сохрани документ «tickets» — каждая задача отдельно: что, где, "
                   "как проверить. Если проблема массовая (на многих страницах) — это правка шаблона сайта, так и пиши. "
                   "Не выдумывай данных, которых нет в отчёте.\n\n" + report[:30000])
    return f"проверено {len(reports)}, критичных {crit}", out


# ---------------- сводка для /seo_status ----------------

def status_text() -> str:
    names = {"hourly": "каждый час", "new_pages": "новые страницы", "daily": "ежедневно", "weekly": "раз в неделю",
             "monthly": "разбор за месяц"}
    lines = [f"🔎 SEO-мониторинг {config.SEO_SITE}"]
    for r in store.runs():
        when = dt.datetime.fromtimestamp(r["ts"]).strftime("%d.%m %H:%M")
        lines.append(f"{'✅' if r['ok'] else '❌'} {names.get(r['name'], r['name'])} — {when}: {r['note'][:150]}")
    al = store.active_alerts()
    lines.append("\nАктивные проблемы:\n" + ("\n".join(f"— {a['text']}" for a in al) if al else "нет"))
    lines.append(f"\nАдресов в sitemap: {len(store.known_urls())}; авто-оформлений сегодня: "
                 f"{_optimized_today()}/{config.SEO_AUTO_OPTIMIZE_PER_DAY}")
    lines.append("Вебмастер и Метрика: " + ("подключены" if apis.has_yandex() else "нет токена"))
    lines.append("IndexNow: " + ("подключён" if config.INDEXNOW_KEY else "нет ключа"))
    return "\n".join(lines)


# ---------------- раз в месяц: разбор результатов и обучение ----------------

async def monthly() -> str:
    """Что из статей собирает поисковый трафик, а что нет → эталоны агенту и предложение новых правил.

    1. Топ статей по поисковому трафику за 30 дней → документ «Лучшие статьи» в знаниях SEO-агента (он видит их в каждой задаче).
    2. Статьи, которые агент оформлял 30+ дней назад: трафик до и после.
    3. Запросы из Вебмастера: где мы растём, где рядом с топом.
    4. Задача SEO-агенту: найти закономерности и предложить правила (propose_rules) — после «Ок» они вступят в силу.
    """
    if not (apis.has_yandex() and config.METRIKA_COUNTER_ID):
        return "нужна Метрика (YANDEX_OAUTH_TOKEN и METRIKA_COUNTER_ID) — разбор пропущен"
    today = dt.date.today()
    end = today - dt.timedelta(days=1)
    cur = {page.norm(u): v for u, v in (await apis.organic_by_page(end - dt.timedelta(days=29), end, 5000) or {}).items()}
    prev = {page.norm(u): v for u, v in (await apis.organic_by_page(end - dt.timedelta(days=59), end - dt.timedelta(days=30), 5000) or {}).items()}
    rows = {r["url"]: r for r in (dict(x) for x in store.db().execute(
        "SELECT url, title, h1, first_seen, optimized FROM urls WHERE in_sitemap=1"))}
    content = {u: r for u, r in rows.items() if is_content(u)}

    top = sorted(((u, v) for u, v in cur.items() if u in content), key=lambda x: -x[1])[:15]
    from core import knowledge
    kd = knowledge.kdir("seo") / "docs"
    kd.mkdir(parents=True, exist_ok=True)
    if top:
        (kd / "top-articles.md").write_text(
            f"Лучшие статьи dsgners по поисковому трафику за 30 дней (обновлено {today:%d.%m.%Y}). "
            "Это эталоны: когда пишешь или оформляешь статью, смотри, как устроены они (read_page).\n\n"
            + "\n".join(f"- {content[u]['title'] or content[u]['h1'] or '(без заголовка)'} — {u} — {v} визитов из поиска" for u, v in top),
            "utf-8")

    month_ago = time.time() - 30 * 86400
    ours = [(u, r) for u, r in content.items() if r.get("optimized") and r["optimized"] < month_ago]
    L = [f"# Разбор результатов SEO за месяц — {today:%d.%m.%Y}", "",
         f"Поисковый трафик на статьи за 30 дней: {sum(v for u, v in cur.items() if u in content)} визитов "
         f"(30 дней до этого: {sum(v for u, v in prev.items() if u in content)}).", ""]
    if top:
        L.append("## Топ статей по трафику из поиска")
        L += [f"- {v} визитов (было {prev.get(u, 0)}) — {content[u]['title'] or '(без заголовка)'} — {u}" for u, v in top]
        L.append("")
    grew = sorted(((u, prev.get(u, 0), v) for u, v in cur.items() if u in content and v >= prev.get(u, 0) + 20),
                  key=lambda x: x[1] - x[2])[:10]
    fell = sorted(((u, p, cur.get(u, 0)) for u, p in prev.items() if u in content and p >= 30 and cur.get(u, 0) < p * 0.7),
                  key=lambda x: x[2] - x[1])[:10]
    if grew:
        L.append("## Выросли сильнее всего")
        L += [f"- {p} → {c} — {content[u]['title'] or '(без заголовка)'} — {u}" for u, p, c in grew]
        L.append("")
    if fell:
        L.append("## Просели больше чем на 30%")
        L += [f"- {p} → {c} — {content[u]['title'] or '(без заголовка)'} — {u}" for u, p, c in fell]
        L.append("")
    if ours:
        L.append("## Статьи, которые ты оформлял (30+ дней назад): трафик за 30 дней")
        L += [f"- {cur.get(u, 0)} визитов (до этого {prev.get(u, 0)}) — {r['title'] or '(без заголовка)'} — {u}"
              for u, r in sorted(ours, key=lambda x: -cur.get(x[0], 0))[:30]]
        L.append("")
    try:
        qs = await apis.wm_queries(28) or []
        if qs:
            L.append("## Запросы из Вебмастера за 4 недели (топ по показам)")
            L += [f"- «{q['query']}» — позиция {q['pos']:.1f}, показов {q['shows']}, кликов {q['clicks']}" for q in qs[:40]]
            L.append("")
    except Exception as e:
        L.append(f"_Вебмастер недоступен: {e}_")
    report = "\n".join(L)
    out = config.OUTBOX / "seo-monthly" / f"seo-review-{today:%Y-%m}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, "utf-8")
    await say(f"📈 Месячный разбор SEO: трафик на статьи за 30 дней — "
              f"{sum(v for u, v in cur.items() if u in content)} визитов. Топ-{len(top)} статей стали эталонами для агента; "
              "выводы и новые правила пришлёт SEO-шник.", [out])
    if _start_job:
        _start_job("seo", f"Разбор результатов за месяц ({today:%m.%Y}): выводы и правила", "monitor",
                   "Ниже данные о поисковом трафике статей dsgners за месяц. Задача — понять, какие статьи и почему "
                   "собирают трафик, и чему научиться.\n"
                   "1. read_page 3–5 лучших статей и 3–5 просевших/слабых. Сравни: тип запроса (информационный, "
                   "подборка, сравнение, кейс), структура, объём, ответ в начале, примеры работ, свежесть, оформление.\n"
                   "2. save_document «review»: 3–6 закономерностей, каждая — с конкретными статьями и цифрами из данных. "
                   "Что обновить в первую очередь (просевшие статьи с потенциалом) и какие темы стоит писать.\n"
                   "3. propose_rules: 3–7 правил для себя, только подтверждённые данными. Формат — конкретное указание "
                   "(«в подборках инструментов — таблица сравнения с ценами в начале»), а не общие слова.\n"
                   "Если данных мало для выводов — так и скажи, правила не выдумывай.\n4. finish.\n\n" + report[:30000])
    return f"статей с трафиком {len(top)}, оформленных агентом {len(ours)}"
