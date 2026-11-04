"""Советы автору в день публикации: новая статья автора → за ~30 минут проверка → письмо автору с советами.

Запускается из проверки новых страниц (каждые 30 минут). Один запрос к модели на статью (~1–2 цента), без агента.
Берёт: автопроверку страницы (title, description, H1/H2, alt, объём), спрос по теме (Вордстат, Вебмастер)
и сам текст. Если править почти нечего — письмо не пишет. Иначе готовит:
  — письмо автору (3–5 конкретных советов) — уходит после «Ок» владельца (или сразу, если SEO_TIPS_AUTO_SEND=1 и есть API);
  — SEO-поля (title, description, OG, alt), которые платформа вправе поправить сама.

Статьи редакции (SEO_OWN_AUTHORS) не трогает — их оформляем сами. Включение: /seo_tips on|off (по умолчанию включено).
"""
import datetime as dt
import json
import logging
import re
import time

import anthropic

from core import config, spend
from core.seo import keywords, page, store

log = logging.getLogger("seo.tips")

TOOL = {
    "name": "author_tips",
    "description": "Итог проверки новой статьи автора.",
    "input_schema": {"type": "object", "properties": {
        "worth_sending": {"type": "boolean", "description": "есть 2+ правки, которые реально прибавят поискового трафика"},
        "why_not": {"type": "string", "description": "если писать не стоит — почему (статья и так хорошая, это не статья, новость-однодневка…)"},
        "main_query": {"type": "string", "description": "как люди ищут эту тему"},
        "subject": {"type": "string", "description": "тема письма до 80 знаков"},
        "letter": {"type": "string", "description": "письмо автору до 1200 знаков"},
        "seo_title": {"type": "string"}, "meta_description": {"type": "string"},
        "og_title": {"type": "string"}, "og_description": {"type": "string"},
        "images": {"type": "array", "items": {"type": "object", "properties": {
            "src": {"type": "string"}, "alt": {"type": "string"}}, "required": ["src", "alt"]}},
    }, "required": ["worth_sending"]},
}

SYSTEM = """Ты — SEO-редактор dsgners.ru (платформа и медиа для дизайнеров). Автор только что опубликовал статью.
Задача — за минуту понять, что ему поправить, чтобы статья попала в топ поиска и собрала читателей, и написать ему.

Что смотреть:
- Отвечает ли начало статьи на главный запрос (первые 2–3 предложения), или там «вода».
- Подзаголовки: есть ли H2/H3 и сформулированы ли они так, как ищут люди.
- Конкретика: примеры, цифры, скриншоты, свой опыт — или общие слова, которые ИИ-поиск перескажет сам.
- Чего не хватает по сравнению с тем, что ищут (запросы по теме даны) — раздел, сравнение, шаги.
- Картинки без подписей и alt, слишком короткий текст, нет вывода.

Правила письма:
- От «Редакции dsgners», на «вы», коротко и по-человечески. Начни с одной фразы, что статья хорошая/интересная
  и чем именно (конкретно, без лести), потом «чтобы её чаще находили в поиске, попробуйте:».
- 3–5 конкретных советов списком: что и где сделать, с примером формулировки. Не «добавьте ключевые слова»,
  а «назовите второй раздел „Как выбрать шрифт для логотипа“ — так это ищут».
- Без SEO-жаргона (не «семантика», «ключи», «сниппет»), без давления, без канцелярита. До 1200 знаков.
- В конце: редакция уже обновила описание статьи для поиска (если ты предлагаешь SEO-поля).
- Только факты из статьи. Не выдумывай цифры, имена, сервисы.

worth_sending=false, если: статья и так хорошо устроена (советы будут косметикой), это короткая новость/анонс
на один день, это не статья (вакансия, объявление), текст пустой или на другом языке.

SEO-поля (если стоит поправить): seo_title 45–60 знаков, главный запрос ближе к началу, без названия сайта
(сайт дописывает « — дизайнерс» сам); meta_description 120–160 знаков; alt — по подписи и тексту вокруг.
Если текущее поле хорошее — верни его как есть."""


def enabled() -> bool:
    return bool(store.get("author_tips", config.SEO_AUTHOR_TIPS))


def _today_count() -> int:
    return int(store.get(f"tips:{dt.date.today()}", 0))


def _bump():
    store.put(f"tips:{dt.date.today()}", _today_count() + 1)


async def check(url: str, rep: page.PageReport | None = None) -> dict:
    """Проверить статью и подготовить письмо. Вернёт {"skip": причина} или {"letter":…, "fields":…, ...}."""
    from core.seo.bulk import _imgs_without_alt
    f = await page.fetch(url)
    if f.status != 200:
        return {"skip": f"HTTP {f.status}"}
    rep = page.analyze(f, is_article=True)
    body = m.group(1) if (m := re.search(r"(?is)<(?:article|main)\b[^>]*>(.*)</(?:article|main)>", f.html)) else f.html
    text = page.strip_html(body)[:9000]
    imgs = _imgs_without_alt(body, f.final, limit=10)
    topic = f"{rep.title} {(rep.h1 or [''])[0]}"
    ws = keywords.search(topic, 15)
    t = keywords.stems(topic)
    wq = [q for q in (store.get("wm_queries") or []) if len(t & keywords.stems(q["query"])) >= 2][:15]
    prompt = (f"Адрес: {url}\nH1: {(rep.h1 or [''])[0]}\nH2: {' | '.join(rep.h2[:15]) or 'нет'}\n"
              f"title: {rep.title}\ndescription: {rep.description or '—'}\nog:title: {rep.og.get('og:title', '—')}\n"
              f"Слов: ~{rep.words}; картинок: {rep.images}, без alt: {len(rep.images_no_alt)}\n"
              f"Автопроверка: {'; '.join(t for l, _, t in rep.issues if l != 'info') or 'без серьёзных проблем'}\n\n"
              "Спрос по теме (Вордстат, в месяц): " + (", ".join(f"«{k['phrase']}» {k['freq']}" for k in ws) or "нет данных")
              + "\nЗапросы из Вебмастера по теме: " + (", ".join(f"«{q['query']}» поз.{q['pos']:.0f}" for q in wq) or "нет данных")
              + "\n\nКартинки без alt:\n" + ("\n".join(f"- {i['src']} (подпись: {i['caption'] or '—'}; перед ней: …{i['context'][-150:]})"
                                                     for i in imgs) or "нет")
              + f"\n\nТекст статьи:\n{text}")
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    model = config.SEO_BULK_MODEL or config.MODEL
    resp = await client.messages.create(
        model=model, max_tokens=2500, system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        tools=[TOOL], tool_choice={"type": "tool", "name": "author_tips"},
        messages=[{"role": "user", "content": prompt}])
    cost = spend.record("seo", resp.usage, model)
    out = next((b.input for b in resp.content if b.type == "tool_use"), None) or {}
    if not out.get("worth_sending") or not out.get("letter"):
        return {"skip": out.get("why_not") or "править почти нечего", "cost": cost}
    from core.seo.bulk import fit_limits
    fields = fit_limits({k: out[k] for k in ("seo_title", "meta_description") if out.get(k)})
    if out.get("images"):
        fields["images"] = [i for i in out["images"] if i.get("src") and i.get("alt")]
    return {"letter": {"url": url, "subject": (out.get("subject") or "Советы по вашей статье")[:100],
                       "text": out["letter"].strip()[:1500]},
            "fields": dict(fields, url=url) if fields else None,
            "main_query": out.get("main_query", ""), "title": (rep.h1 or [rep.title])[0], "cost": cost}


async def run_for(reports: list, is_own, author_of) -> str:
    """Из проверки новых страниц: по каждой новой статье автора — письмо на «Ок» владельцу."""
    if not enabled():
        return ""
    from core import site_api
    from core.office import office
    from core import runner
    made = skipped = 0
    for r in reports:
        if is_own(r.url) or store.get(f"tips_done:{r.url}") or _today_count() >= config.SEO_AUTHOR_TIPS_PER_DAY:
            continue
        store.put(f"tips_done:{r.url}", time.time())
        _bump()
        try:
            res = await check(r.url)
        except Exception as e:
            log.exception("tips")
            office.emit("seo", "error", f"Советы автору не получились ({r.url}): {e}")
            continue
        if res.get("skip"):
            skipped += 1
            office.emit("seo", "info", f"Новая статья {author_of(r.url)}: письмо не нужно — {res['skip']}")
            continue
        # оформляем как задачу SEO-шника на согласовании — дальше обычные «Ок / Переделать» и отправка через API
        L = res["letter"]
        task = f"Советы автору {author_of(r.url)}: {res['title'][:60]}"
        jid = office.create_job("seo", task, task, "monitor")
        d = config.OUTBOX / f"{dt.datetime.now():%Y%m%d-%H%M%S}-seo-tips-{jid[:6]}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "author-letter.md").write_text(f"# {L['subject']}\n\nСтатья: {r.url}\n\n{L['text']}", "utf-8")
        meta = {"letter": L}
        if res.get("fields"):
            meta["seo_fields"] = res["fields"]
        caption = (f"Новая статья автора {author_of(r.url)} — письмо с советами готово"
                   + (f" (главный запрос: {res['main_query']})" if res.get("main_query") else ""))
        office.update_job(jid, status="review", files=[str((d / "author-letter.md").relative_to(config.OUTBOX))],
                          caption=caption, cost=res.get("cost", 0), meta=meta, finished=time.time())
        office.emit("seo", "approval", f"Письмо автору {author_of(r.url)}: жду «Ок»", jid)
        made += 1
        job = office.get_job(jid)
        if config.SEO_TIPS_AUTO_SEND and site_api.enabled():
            runner.approve(jid)     # без согласования: сразу на сайт и автору
        else:
            for cb in runner._listeners:
                try:
                    await cb(job)
                except Exception:
                    log.exception("listener")
    parts = []
    if made:
        parts.append(f"письма авторам: {made}")
    if skipped:
        parts.append(f"без письма (и так хорошо): {skipped}")
    return ", ".join(parts)
