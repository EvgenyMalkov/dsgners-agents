"""Подборки статей под блоки подстраниц хаба (каркас «Кластер Брендинг»).

/cluster_pick logo: все статьи подтемы → дешёвая модель кладёт каждую максимум в один блок страницы (или никуда:
дайджесты, анонсы, не по теме) и ставит силу совпадения 1–3 → внутри блока сортировка по силе и визитам из поиска
(Метрика, 28 дней). Отчёт — markdown на утверждение редактору; результат сохраняется (kv pick:<ключ>).
"""
import datetime as dt
import json
import logging
import re

import anthropic

from core import config, spend
from core.seo import apis, inventory, page, store

log = logging.getLogger("seo.pick")
BATCH = 60

# ключ → (направление, подтема в inventory.T, адрес, [(H2, что сюда подходит)])
SUBPAGES = {
    "branding": ("Брендинг", "Брендинг (общее)", "/education/branding", [
        ("Что такое брендинг простыми словами", "объяснения, что такое брендинг, виды, архетипы, отличие от айдентики и маркетинга"),
        ("Тренды брендинга 2026", "тренды, обзоры направлений, современный брендинг"),
        ("Кейсы брендинга", "кейсы студий и дизайнеров: разработка бренда для конкретной компании"),
        ("Нейминг: как придумывают названия брендов", "нейминг, придумывание названий брендов (НЕ нейминг слоёв и компонентов в Figma)"),
        ("Книги и курсы о брендинге", "книги, курсы, лекции, отзывы на обучение брендингу"),
    ]),
    "logo": ("Брендинг", "Логотип", "/education/branding/logo", [
        ("Новые логотипы брендов", "новости и разборы смены логотипа известной компании, редизайн логотипа бренда"),
        ("Лучшие логотипы: примеры", "кейсы разработки логотипа для конкретного клиента, подборки удачных логотипов"),
        ("Как создать логотип", "гайды, уроки, советы, с чего начать, сервисы для создания логотипа, процесс работы"),
        ("Сетка, пропорции, золотое сечение", "построение логотипа: модульная сетка, пропорции, золотое сечение, оптика"),
        ("Логотип и нейросети", "нейросети, генераторы, ИИ, промты в работе над логотипом"),
        ("Современные логотипы и тренды", "тренды в логотипах, каким должен быть современный логотип"),
        ("Сколько стоит логотип", "цена логотипа, как оценить работу, из чего складывается стоимость"),
    ]),
    "rebrending": ("Брендинг", "Ребрендинг", "/education/branding/rebrending", [
        ("Что такое ребрендинг простыми словами", "объяснения: что такое ребрендинг, зачем, ребрендинг и рестайлинг"),
        ("Ребрендинги известных брендов", "новости и разборы ребрендинга конкретной известной компании"),
        ("Ребрендинг компании: как проходит", "кейсы студий по ребрендингу клиента, этапы, процесс"),
        ("Удачные и неудачные ребрендинги", "сравнения, провалы, разборы ошибок и удач"),
    ]),
    "firmennyj-stil": ("Брендинг", "Фирменный стиль", "/education/branding/firmennyj-stil", [
        ("Что такое фирменный стиль и из чего состоит", "объяснения, элементы фирменного стиля, отличие от айдентики"),
        ("Разработка фирменного стиля", "как разрабатывают фирменный стиль, процесс, советы, что входит в работу"),
        ("Фирменный стиль компаний: примеры", "кейсы и новости: фирменный стиль конкретной компании"),
        ("Фирменные шрифты и цвета", "фирменный шрифт, фирменный цвет, палитра бренда"),
        ("Мерч и носители", "мерч, сувенирка, носители фирменного стиля, мокапы"),
        ("Тренды фирменного стиля", "тренды в фирменном стиле"),
    ]),
    "aydentika": ("Брендинг", "Айдентика", "/education/branding/aydentika", [
        ("Что такое айдентика простыми словами", "объяснения: что такое айдентика, элементы, виды, отличие от брендинга и фирменного стиля"),
        ("Примеры айдентики брендов", "кейсы: айдентика для конкретного бренда или компании"),
        ("Как разрабатывают айдентику", "процесс разработки айдентики, роль арт-директора, советы"),
        ("Айдентика городов и событий", "айдентика городов, территорий, фестивалей, конференций, мероприятий"),
        ("Айдентика и нейросети", "ИИ и нейросети в работе над айдентикой"),
    ]),
    "brendbuk": ("Брендинг", "Брендбук и гайдлайны", "/education/branding/brendbuk", [
        ("Что такое брендбук и гайдлайн", "объяснения, отличие брендбука от гайдлайна и гайдбука, что входит"),
        ("Примеры брендбуков", "кейсы и публикации, где показан брендбук или гайдлайн бренда"),
        ("Как сделать брендбук", "процесс, структура, шаги, советы по разработке брендбука"),
        ("Гайдлайны в продуктах", "дизайн-системы и гайдлайны цифровых продуктов, иконки, правила интерфейса"),
        ("Сколько стоит брендбук", "цена, оценка работы над брендбуком"),
    ]),
    "upakovka": ("Брендинг", "Упаковка", "/education/branding/upakovka", [
        ("Примеры дизайна упаковки", "кейсы и новости: упаковка конкретного продукта или бренда"),
        ("Как разработать дизайн упаковки", "процесс, советы, гайды по дизайну упаковки и этикетки"),
        ("Тренды в дизайне упаковки", "тренды упаковки"),
        ("Лучшая упаковка: премии", "премии и конкурсы упаковки: Pentawards и другие"),
    ]),
}


def keys() -> str:
    return ", ".join(SUBPAGES)


TOOL = {
    "name": "assign",
    "description": "Для каждой статьи — номер блока страницы (или -1) и сила совпадения.",
    "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {
        "type": "object", "properties": {
            "i": {"type": "integer"},
            "block": {"type": "integer", "description": "номер блока из списка; -1 — никуда"},
            "score": {"type": "integer", "description": "1 — слабо, 2 — хорошо, 3 — точно про этот блок"},
        }, "required": ["i", "block", "score"]}}}, "required": ["items"]},
}


def _items(out) -> list[dict]:
    items = out.get("items", []) if isinstance(out, dict) else []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            return []
    res = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            try:
                it = json.loads(it)
            except ValueError:
                continue
        if isinstance(it, dict) and isinstance(it.get("i"), int) and isinstance(it.get("block"), int):
            res.append({"i": it["i"], "block": it["block"], "score": int(it.get("score") or 1)})
    return res


def _subtopic_posts(direction: str, sub: str) -> list[dict]:
    _, pat = inventory.T[direction][sub]
    rx = re.compile(pat)
    try:
        from core.seo import classify
        llm = classify.results()
    except Exception:
        llm = {}
    lab = f"{direction} / {sub}"
    return [p for p in inventory._posts() if rx.search(p["text"]) or lab in llm.get(p["url"], ())]


async def build(key: str) -> str:
    if key not in SUBPAGES:
        raise ValueError(f"Нет подстраницы «{key}». Есть: {keys()}")
    direction, sub, path, blocks = SUBPAGES[key]
    posts = _subtopic_posts(direction, sub)
    meta = {r["url"]: (r["title"] or "", r["description"] or "") for r in store.db().execute(
        "SELECT url, title, description FROM urls")}
    org = {}
    try:
        end = dt.date.today() - dt.timedelta(days=1)
        org = {page.norm(u): v for u, v in (await apis.organic_by_page(end - dt.timedelta(days=27), end, 20000) or {}).items()}
    except Exception as e:
        log.warning("метрика: %s", e)
    system = (f"Ты собираешь подборки для страницы «{sub}» на дизайн-платформе дизайнерс ({path}). Для каждой статьи выбери "
              "ОДИН блок, куда она подходит лучше всего, или -1. Ставь -1 дайджестам новостей за неделю/месяц, анонсам, "
              "вакансиям и статьям, которые на самом деле не про эту подтему. Силу совпадения ставь честно.\nБлоки:\n"
              + "\n".join(f"{j}. {h} — {d}" for j, (h, d) in enumerate(blocks)))
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    model = config.SEO_CLASSIFY_MODEL or config.MODEL
    picked: dict[int, list] = {j: [] for j in range(len(blocks))}
    skipped = 0
    for k in range(0, len(posts), BATCH):
        chunk = posts[k:k + BATCH]
        lines = []
        for j, p in enumerate(chunk):
            t, d = meta.get(p["url"], ("", ""))
            lines.append(f"{j}. {t or p['title']} — {d[:160]}")
        resp = await client.messages.create(
            model=model, max_tokens=3000, system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[TOOL], tool_choice={"type": "tool", "name": "assign"},
            messages=[{"role": "user", "content": "Статьи:\n" + "\n".join(lines)}])
        spend.record("seo", resp.usage, model)
        out = next((b.input for b in resp.content if b.type == "tool_use"), {}) or {}
        seen = set()
        for it in _items(out):
            i, b = it["i"], it["block"]
            if not 0 <= i < len(chunk) or i in seen:
                continue
            seen.add(i)
            if b not in picked:
                skipped += 1
                continue
            p = chunk[i]
            picked[b].append({"url": p["url"], "title": meta.get(p["url"], ("", ""))[0] or p["title"],
                              "score": max(1, min(3, it["score"])), "visits": org.get(page.norm(p["url"]), 0)})
    # авторы и студии подтемы
    authors: dict[str, int] = {}
    for p in posts:
        a = p["url"].split("/")[3] if p["url"].count("/") >= 4 else ""
        if a:
            authors[a] = authors.get(a, 0) + 1
    today = dt.date.today()
    site = f"https://{config.SEO_SITE}"
    md = [f"# Подборки для {path} — {today:%d.%m.%Y}", "",
          f"Статей в подтеме «{sub}»: {len(posts)}. Модель разложила их по блокам страницы (одна статья — один блок), "
          f"никуда не подошло: {len(posts) - sum(len(v) for v in picked.values())}. "
          "Внутри блока — по силе совпадения (★) и визитам из поиска за 28 дней. На страницу идут первые 3–9; "
          "блок, где меньше 3 статей с ★★ и выше, не показываем.", ""]
    result = {}
    for j, (h, _) in enumerate(blocks):
        rows = sorted(picked[j], key=lambda r: (-r["score"], -r["visits"]))
        strong = [r for r in rows if r["score"] >= 2]
        result[h] = [r["url"] for r in strong[:9]]
        md += [f"## {h}", "", f"Подходит: {len(rows)}, из них уверенно (★★ и выше): {len(strong)}"
               + ("" if len(strong) >= 3 else " — мало, блок пока не показываем"), ""]
        md += [f"{n}. {'★' * r['score']} {r['title']} — {r['visits']} визитов — {r['url'].replace(site, '')}"
               for n, r in enumerate(rows[:12], 1)]
        md.append("")
    md += ["## Кто делает (авторы и студии по числу статей в подтеме)", ""]
    md += [f"- {a} — {c}" for a, c in sorted(authors.items(), key=lambda kv: -kv[1])[:10]]
    store.put(f"pick:{key}", {"date": today.isoformat(), "blocks": result})
    out_path = config.OUTBOX / "seo-plan" / f"pick-{key}-{today:%Y-%m-%d}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(md), "utf-8")
    return str(out_path)
