"""Массовое SEO-оформление всех статей сайта: один дешёвый запрос к модели на статью (~1 цент), без агента.

Как идёт:
1. start()   — берёт статьи с проблемами из последнего обхода (нет/плохой description, title, og, alt),
               ставит в очередь (сначала те, что дают поисковый трафик), делает пробную партию (20 шт.)
               и отдаёт владельцу таблицу «было → стало» с кнопками «Применить ко всем» / «Переделать».
2. «Переделать» с комментарием → комментарий становится правилом массовой обработки, пробная партия заново.
3. «Применить ко всем» → пробная партия уходит на сайт, остальное обрабатывается по расписанию порциями
   (SEO_BULK_PER_HOUR), каждая порция сразу записывается на сайт через API (если подключено), иначе копится
   в CSV для импорта разработчиком. Отчёт о ходе — раз в день.

Текст статей не меняется — только title, description, OpenGraph и alt картинок. Для статей авторов то же самое:
это поля платформы.
"""
import asyncio
import csv
import datetime as dt
import json
import logging
import re
import time
from html import unescape
from pathlib import Path

import anthropic

from core import config, spend
from core.seo import page, store

log = logging.getLogger("seo.bulk")
SAMPLE = 20
FIX_CODES = {"no_description", "desc_short", "desc_long", "no_title", "title_short", "title_long",
             "no_og_title", "no_og_image", "img_no_alt"}


def _db():
    d = store.db()
    d.execute("""CREATE TABLE IF NOT EXISTS bulk(url TEXT PRIMARY KEY, prio REAL, state TEXT, fields TEXT,
                 before TEXT, note TEXT, ts REAL)""")
    return d


def state() -> dict:
    return store.get("bulk_state", {"phase": "idle"})


def _set_state(**kw):
    s = state()
    s.update(kw)
    store.put("bulk_state", s)
    return s


def rules() -> list[str]:
    return store.get("bulk_rules", [])


def add_rule(text: str):
    r = rules()
    r.append(text.strip())
    store.put("bulk_rules", r[-30:])


def counts() -> dict:
    return {r[0]: r[1] for r in _db().execute("SELECT state, COUNT(*) FROM bulk GROUP BY state")}


# ---------- очередь ----------

def candidates(is_content) -> list[tuple[str, list[str]]]:
    """Статьи, у которых последний обход нашёл, что поправить в SEO-полях."""
    out = []
    for url, issues in store.db().execute("SELECT url, issues FROM urls WHERE in_sitemap=1 AND status=200"):
        if not is_content(url):
            continue
        codes = {c for _, c, _ in json.loads(issues or "[]")}
        fix = sorted(codes & FIX_CODES)
        if fix:
            out.append((url, fix))
    return out


async def build_queue(is_content) -> int:
    from core.seo import apis
    traffic = {}
    if apis.has_yandex() and config.METRIKA_COUNTER_ID:
        try:
            end = dt.date.today() - dt.timedelta(days=1)
            traffic = {page.norm(u): v for u, v in
                       (await apis.organic_by_page(end - dt.timedelta(days=29), end, 10000) or {}).items()}
        except Exception as e:
            log.warning("metrika: %s", e)
    d = _db()
    n = 0
    for url, fix in candidates(is_content):
        if d.execute("SELECT 1 FROM bulk WHERE url=?", (url,)).fetchone():
            continue
        d.execute("INSERT INTO bulk(url, prio, state, note, ts) VALUES(?,?,?,?,?)",
                  (url, traffic.get(url, 0), "queued", ",".join(fix), time.time()))
        n += 1
    d.commit()
    return n


# ---------- генерация одной статьи ----------

TOOL = {
    "name": "seo_fields",
    "description": "Итоговые SEO-поля статьи.",
    "input_schema": {"type": "object", "properties": {
        "seo_title": {"type": "string", "description": "45–65 знаков"},
        "meta_description": {"type": "string", "description": "120–160 знаков"},
        "og_title": {"type": "string", "description": "до 70 знаков"},
        "og_description": {"type": "string", "description": "до 200 знаков"},
        "images": {"type": "array", "items": {"type": "object", "properties": {
            "src": {"type": "string"}, "alt": {"type": "string"}}, "required": ["src", "alt"]}},
        "skip": {"type": "string", "description": "если статью оформлять не нужно или нельзя (пустая, не статья) — почему"},
    }, "required": ["seo_title", "meta_description", "og_title", "og_description", "images"]},
}

SYSTEM = """Ты — SEO-редактор dsgners.ru (платформа и медиа для дизайнеров). Делаешь SEO-поля для уже опубликованной статьи.
Текст статьи не меняешь — только поля.

Правила:
- seo_title: 45–65 знаков. Главный запрос (как люди ищут эту тему) ближе к началу. Без капса, кликбейта и кавычек-ёлочек
  по краям. Название сайта в конце сайт добавляет сам (« — дизайнерс») — в seo_title его не пиши.
- meta_description: 120–160 знаков. О чём статья и чем полезна + конкретика из текста (цифра, «по шагам», «примеры»,
  название инструмента). Не повторяй title. Без «В этой статье…».
- og_title / og_description: для превью в соцсетях — можно живее, но по фактам статьи.
- images: alt для каждой картинки из списка — что на ней по существу (по подписи, имени файла и тексту вокруг),
  до 120 знаков, без «картинка», «изображение», без спама ключами. Не знаешь, что на картинке — опиши по контексту раздела.
- Если текущее значение поля уже хорошее (в нужной длине, по делу) — верни его без изменений.
- Только факты из статьи. Никаких выдуманных цифр, имён, обещаний.
- Русский язык, «ё» где нужно."""


def _imgs_without_alt(html: str, base: str, limit=15) -> list[dict]:
    out = []
    for m in re.finditer(r"(?is)<img\b[^>]*>", html):
        tag = m.group(0)
        src = page.attr(tag, "src") or page.attr(tag, "data-src")
        if not src or src.startswith("data:") or page.has_alt(tag):
            continue
        before = page.strip_html(html[max(0, m.start() - 1500):m.start()])[-250:]
        cap = re.search(r"(?is)<figcaption[^>]*>(.*?)</figcaption>", html[m.end():m.end() + 600])
        from urllib.parse import urljoin
        out.append({"src": urljoin(base, src), "context": before.replace("\n", " "),
                    "caption": page.strip_html(cap.group(1)) if cap else ""})
        if len(out) >= limit:
            break
    return out


async def generate(url: str, client=None) -> dict:
    f = await page.fetch(url)
    if f.status != 200:
        return {"skip": f"страница не открылась: HTTP {f.status} {f.error}"}
    rep = page.analyze(f, is_article=True)
    body = m.group(1) if (m := re.search(r"(?is)<(?:article|main)\b[^>]*>(.*)</(?:article|main)>", f.html)) else f.html
    text = page.strip_html(body)[:7000]
    imgs = _imgs_without_alt(body, f.final)
    before = {"seo_title": rep.title, "meta_description": rep.description,
              "og_title": rep.og.get("og:title", ""), "og_description": rep.og.get("og:description", "")}
    extra = "\n".join(f"- {r}" for r in rules())
    prompt = (f"Адрес: {url}\nH1: {(rep.h1 or [''])[0]}\n\nТекущие поля:\n"
              + "\n".join(f"{k} ({len(v)} симв.): {v or '—'}" for k, v in before.items())
              + f"\n\nНайденные проблемы: {', '.join(t for _, _, t in rep.issues if _ != 'info') or 'нет'}"
              + "\n\nКартинки без alt:\n" + ("\n".join(
                  f"- {i['src']}\n  подпись: {i['caption'] or '—'}\n  текст перед ней: …{i['context']}" for i in imgs) or "нет")
              + f"\n\nТекст статьи:\n{text}")
    sys_text = SYSTEM + (f"\n\nПравила владельца (главнее остальных):\n{extra}" if extra else "")
    client = client or anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    model = config.SEO_BULK_MODEL or config.MODEL
    resp = await client.messages.create(
        model=model, max_tokens=1500, system=[{"type": "text", "text": sys_text, "cache_control": {"type": "ephemeral"}}],
        tools=[TOOL], tool_choice={"type": "tool", "name": "seo_fields"},
        messages=[{"role": "user", "content": prompt}])
    spend.record("seo", resp.usage, model)
    out = next((b.input for b in resp.content if b.type == "tool_use"), None) or {}
    if out.get("skip"):
        return {"skip": out["skip"]}
    out["images"] = [i for i in out.get("images") or [] if i.get("src") and i.get("alt")]
    # жёсткие проверки длины — модели тоже ошибаются
    if len(out.get("seo_title", "")) > 75:
        out["seo_title"] = out["seo_title"][:72].rsplit(" ", 1)[0]
    if len(out.get("meta_description", "")) > 180:
        out["meta_description"] = out["meta_description"][:175].rsplit(" ", 1)[0] + "…"
    return {"fields": out, "before": before}


async def process(urls: list[str], concurrency=3) -> int:
    sem = asyncio.Semaphore(concurrency)
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    d = _db()
    done = 0

    async def one(u):
        nonlocal done
        async with sem:
            try:
                r = await generate(u, client)
            except Exception as e:
                d.execute("UPDATE bulk SET state='error', note=?, ts=? WHERE url=?", (str(e)[:300], time.time(), u))
                return
            if r.get("skip"):
                d.execute("UPDATE bulk SET state='skipped', note=?, ts=? WHERE url=?", (r["skip"][:300], time.time(), u))
            else:
                d.execute("UPDATE bulk SET state='ready', fields=?, before=?, ts=? WHERE url=?",
                          (json.dumps(r["fields"], ensure_ascii=False), json.dumps(r["before"], ensure_ascii=False),
                           time.time(), u))
                done += 1
            d.commit()

    await asyncio.gather(*(one(u) for u in urls))
    return done


# ---------- применение ----------

async def apply_ready(limit=1000) -> tuple[int, int, str]:
    """Готовые поля → на сайт (если есть API), иначе в CSV. Вернёт (записано, ошибок, путь к CSV или '')."""
    from core import site_api
    d = _db()
    rows = d.execute("SELECT url, fields FROM bulk WHERE state='ready' ORDER BY prio DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return 0, 0, ""
    if not site_api.enabled():
        return 0, 0, ""          # без API копим готовое и отдаём одним CSV в конце
    ok = err = 0
    for url, fields in rows:
        try:
            art = await site_api.get_article(url)
            store.put(f"seo_before:{art['id']}", dict(art.get("seo") or {}, images=art.get("images") or []))
            await site_api.update_seo(art["id"], json.loads(fields))
            d.execute("UPDATE bulk SET state='applied', ts=? WHERE url=?", (time.time(), url))
            store.mark_optimized(url)
            ok += 1
        except Exception as e:
            d.execute("UPDATE bulk SET state='error', note=?, ts=? WHERE url=?", (f"запись: {e}"[:300], time.time(), url))
            err += 1
        d.commit()
        await asyncio.sleep(0.3)
    return ok, err, ""


def export_csv(states=("ready",)) -> Path:
    """Таблица для импорта разработчиком (пока нет API) и для просмотра «было → стало»."""
    out = config.OUTBOX / "seo-bulk" / f"seo-fields-{dt.datetime.now():%Y%m%d-%H%M}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    q = ",".join("?" * len(states))
    rows = _db().execute(f"SELECT url, before, fields FROM bulk WHERE state IN ({q}) ORDER BY prio DESC", states).fetchall()
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["url", "title было", "title стало", "description было", "description стало",
                    "og_title", "og_description", "alt картинок (json)"])
        for url, before, fields in rows:
            b, f = json.loads(before or "{}"), json.loads(fields or "{}")
            w.writerow([url, b.get("seo_title", ""), f.get("seo_title", ""), b.get("meta_description", ""),
                        f.get("meta_description", ""), f.get("og_title", ""), f.get("og_description", ""),
                        json.dumps(f.get("images") or [], ensure_ascii=False)])
    return out


# ---------- этапы ----------

async def run_sample(is_content) -> tuple[str, Path | None]:
    """Очередь + пробная партия. Вернёт (сообщение, файл «было → стало»)."""
    added = await build_queue(is_content)
    d = _db()
    total = d.execute("SELECT COUNT(*) FROM bulk WHERE state IN ('queued','sample')").fetchone()[0]
    if not total:
        return ("Статей с проблемами в SEO-полях не нашёл. Если недельного обхода ещё не было — "
                "запусти /seo_scan weekly и потом /seo_bulk."), None
    d.execute("UPDATE bulk SET state='queued', fields=NULL WHERE state IN ('sample','sample_ready')")
    urls = [r[0] for r in d.execute("SELECT url FROM bulk WHERE state='queued' ORDER BY prio DESC, ts LIMIT ?", (SAMPLE,))]
    for u in urls:
        d.execute("UPDATE bulk SET state='sample' WHERE url=?", (u,))
    d.commit()
    before_cost = spend.this_month()
    await process(urls)
    for u in urls:   # готовые в пробной партии помечаем отдельно, чтобы не ушли на сайт до «Ок»
        d.execute("UPDATE bulk SET state='sample_ready' WHERE url=? AND state='ready'", (u,))
    d.commit()
    cost = spend.this_month() - before_cost
    per = cost / max(len(urls), 1)
    _set_state(phase="sample", total=total, started=time.time())
    path = export_csv(("sample_ready",))
    eta_days = total / max(config.SEO_BULK_PER_HOUR * 24, 1)
    return (f"🧹 Массовое SEO-оформление: статей с проблемами — {total} (новых в очереди: {added}).\n"
            f"Пробная партия: {len(urls)} статей, в файле «было → стало».\n"
            f"Стоимость: ~${per:.3f} за статью → всего ~${per * total:.0f}; "
            f"займёт ~{max(1, round(eta_days))} дн. по {config.SEO_BULK_PER_HOUR} в час.\n"
            "Текст статей не меняется — только title, description, OpenGraph и alt."), path


def approve() -> str:
    d = _db()
    d.execute("UPDATE bulk SET state='ready' WHERE state='sample_ready'")
    d.execute("UPDATE bulk SET state='queued' WHERE state='sample'")
    d.commit()
    _set_state(phase="running", approved=time.time())
    return "running"


async def tick(say=None) -> str:
    """Порция по расписанию (раз в час): сгенерировать и записать на сайт. say(text, files) — сообщение владельцу."""
    from core import site_api
    s = state()
    if s.get("phase") != "running":
        return "не запущено"
    if spend.this_month() >= config.MONTHLY_LIMIT_USD * 0.9:
        return "пауза: расходы близки к месячному лимиту"
    d = _db()
    urls = [r[0] for r in d.execute("SELECT url FROM bulk WHERE state='queued' ORDER BY prio DESC, ts LIMIT ?",
                                    (config.SEO_BULK_PER_HOUR,))]
    n = await process(urls) if urls else 0
    ok, err, _ = await apply_ready()
    left = d.execute("SELECT COUNT(*) FROM bulk WHERE state='queued'").fetchone()[0]
    note = f"сгенерировано {n}, записано на сайт {ok}, ошибок {err}, осталось {left}"
    today = f"{dt.date.today()}"
    if not left:
        _set_state(phase="done", finished=time.time())
        files = [] if site_api.enabled() else [export_csv(("ready",))]
        if say:
            await say("✅ Массовое SEO-оформление закончено.\n" + status_text()
                      + ("" if site_api.enabled() else "\nAPI сайта нет — файл для импорта разработчику приложен."), files)
    elif say and store.get("bulk_last_report") != today:
        store.put("bulk_last_report", today)
        await say("🧹 " + status_text(), [])
    return note


def status_text() -> str:
    s, c = state(), counts()
    names = {"idle": "не запускалось", "sample": "ждёт «Ок» на пробную партию", "running": "идёт", "done": "готово"}
    parts = [f"🧹 Массовое SEO: {names.get(s.get('phase'), s.get('phase'))}"]
    if c:
        lab = {"queued": "в очереди", "sample_ready": "в пробной партии", "ready": "готово к записи",
               "applied": "записано на сайт", "skipped": "пропущено", "error": "ошибки"}
        parts.append(", ".join(f"{lab.get(k, k)}: {v}" for k, v in sorted(c.items())))
    if rules():
        parts.append("Правила: " + "; ".join(rules()[-5:]))
    return "\n".join(parts)
