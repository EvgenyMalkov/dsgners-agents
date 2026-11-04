"""Карта спроса по разделам SEO-стратегии (Вордстат API): хабы и референсы, курсы, студии по городам,
глоссарий, инструменты, вакансии и зарплаты.

По каждой фразе: частота за 30 дней, рост к прошлому году и за 3 месяца, сезонный пик, топ-5 уточнений.
Для студий — ещё города, где ищут. Итог — markdown-файл + CSV в outbox/seo-plan, фразы ложатся в базу семантики.
Запуск: /ws_map в боте (несколько минут, ~150–200 запросов к API, ответы кешируются на 3 дня).
"""
import csv
import datetime as dt
import logging

from core import config
from core.seo import wordstat

log = logging.getLogger("seo.demand_map")

SECTIONS = {
    "Хабы тем и подборки референсов": [
        "ux ui дизайн", "веб дизайн", "графический дизайн", "айдентика", "продуктовый дизайн", "моушн дизайн",
        "3d дизайн", "дизайн мобильного приложения", "дизайн презентации", "портфолио дизайнера",
        "примеры лендингов", "примеры онбординга", "референсы для дизайна", "ui kit", "дизайн система",
        "типографика", "иллюстрация",
    ],
    "Каталог курсов": [
        "курсы ux ui дизайна", "курсы графического дизайна", "курсы веб дизайна", "курсы моушн дизайна",
        "курсы 3d дизайна", "курсы продуктового дизайна", "курсы figma", "курсы дизайна бесплатно",
        "обучение дизайну с нуля", "курсы иллюстрации", "курсы дизайна презентаций",
    ],
    "Каталог студий (услуга × город)": [
        "брендинговое агентство", "студия веб дизайна", "разработка логотипа", "разработка фирменного стиля",
        "дизайн студия", "заказать дизайн сайта", "разработка брендбука", "дизайн упаковки",
    ],
    "Глоссарий": [
        "кернинг", "гайдлайн", "айдентика это", "брендбук", "вайрфрейм", "прототип сайта", "юзабилити",
        "cjm", "ux это", "ui это", "дизайн система это", "типографика это", "модульная сетка", "мудборд",
        "референс это", "лендинг это", "адаптивный дизайн", "контраст цветов", "цветовой круг", "фирменный стиль",
    ],
    "Инструменты": [
        "проверка контраста цветов", "генератор палитры", "подбор цветов", "сочетание цветов", "сочетание шрифтов",
        "сколько стоит логотип", "сколько стоит сайт", "стоимость дизайна", "мокапы", "иконки бесплатно",
        "шаблоны презентаций", "генератор логотипа", "калькулятор стоимости сайта",
    ],
    "Вакансии и зарплаты": [
        "вакансии дизайнер", "вакансии ux ui дизайнер", "вакансии графический дизайнер", "работа дизайнером удаленно",
        "зарплата дизайнера", "зарплата ux ui дизайнера", "резюме дизайнера", "фриланс дизайнер",
    ],
}
CITIES_FOR = {"Каталог студий (услуга × город)"}


def _n(x: int) -> str:
    return f"{x:,}".replace(",", " ")


async def build(progress=None) -> tuple[str, str]:
    """Собрать карту. Вернёт (путь к md, путь к csv)."""
    today = dt.date.today()
    out = config.OUTBOX / "seo-plan"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    md = [f"# Карта спроса дизайнерс — Вордстат, {today:%d.%m.%Y}", "",
          "Частота — запросов за 30 дней со всеми словами фразы (широкая). Год — последний месяц к тому же месяцу "
          "прошлого года; 3 мес — последние 3 месяца к 3 предыдущим.", ""]
    for section, phrases in SECTIONS.items():
        if progress:
            await progress(f"Вордстат: {section}")
        md += [f"## {section}", "", "| Фраза | Частота | Год | 3 мес | Пик | Главные уточнения |", "|---|---|---|---|---|---|"]
        srows = []
        for p in phrases:
            r = {"section": section, "phrase": p}
            try:
                t = await wordstat.top(p, 200)
                r["total"] = t["total"]
                r["subs"] = "; ".join(f"{x['phrase']} {x['count']}" for x in t["results"][1:6])
                r.update(wordstat.trend(await wordstat.dynamics(p)))
                if section in CITIES_FOR:
                    reg = await wordstat.regions(p, "cities", limit=8)
                    r["cities"] = "; ".join(f"{x['region']} {x['count']} (инд. {x['affinity']})" for x in reg)
            except Exception as e:
                log.warning("%s: %s", p, e)
                r["error"] = str(e)[:120]
            srows.append(r)
        srows.sort(key=lambda r: -r.get("total", 0))
        for r in srows:
            if "error" in r:
                md.append(f"| {r['phrase']} | ошибка: {r['error']} | | | | |")
                continue
            yoy = f"{r['yoy']:+d}%" if "yoy" in r else "—"
            q = f"{r['q']:+d}%" if "q" in r else "—"
            md.append(f"| {r['phrase']} | {_n(r['total'])} | {yoy} | {q} | {r.get('peak', '—')} | {r.get('subs', '')} |")
        cities = [r for r in srows if r.get("cities")]
        if cities:
            md += ["", "Города, где ищут (индекс > 100 — чаще среднего по стране):", ""]
            md += [f"- **{r['phrase']}**: {r['cities']}" for r in cities]
        total = sum(r.get("total", 0) for r in srows)
        md += ["", f"Сумма по разделу: {_n(total)} запросов за 30 дней (фразы пересекаются, это ориентир, а не точный объём).", ""]
        rows += srows
    mdp = out / f"demand-map-{today:%Y-%m-%d}.md"
    mdp.write_text("\n".join(md), "utf-8")
    csvp = out / f"demand-map-{today:%Y-%m-%d}.csv"
    with open(csvp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["раздел", "фраза", "частота_30д", "год_%", "3мес_%", "пик", "уточнения", "города", "ошибка"])
        for r in rows:
            w.writerow([r["section"], r["phrase"], r.get("total", ""), r.get("yoy", ""), r.get("q", ""),
                        r.get("peak", ""), r.get("subs", ""), r.get("cities", ""), r.get("error", "")])
    return str(mdp), str(csvp)


# ---------------- хабы: спрос × что у нас уже есть ----------------
HUBS = {
    "ui": ["ui дизайн", "ui дизайнер", "дизайн интерфейсов", "ui kit", "ux ui дизайн"],
    "ux": ["ux дизайн", "ux исследования", "юзабилити", "cjm", "ux дизайнер"],
    "graphic-design": ["графический дизайн", "графический дизайнер", "графический дизайн для начинающих", "дизайн плаката"],
    "3d": ["3d дизайн", "3d моделирование", "blender", "3d визуализация", "3d графика"],
    "career": ["работа дизайнером", "вакансии дизайнер", "зарплата дизайнера", "портфолио дизайнера",
               "фриланс дизайнер", "резюме дизайнера"],
    "typography": ["типографика", "шрифты для дизайна", "сочетание шрифтов", "кернинг", "леттеринг"],
    "motion": ["моушн дизайн", "анимация для дизайнера", "after effects", "моушн дизайнер"],
    "product-design": ["продуктовый дизайн", "продуктовый дизайнер", "дизайн продукта"],
    "branding": ["брендинг", "айдентика", "фирменный стиль", "разработка логотипа", "брендбук", "нейминг"],
    "ai": ["нейросети для дизайна", "нейросеть для дизайнеров", "ии в дизайне", "midjourney",
           "нейросеть для логотипа", "нейросеть для создания дизайна"],
    "kreativ": ["креатив", "креативная реклама", "креативные идеи для рекламы"],
}


async def build_hubs() -> str:
    """По каждой теме сайта: спрос (Вордстат), наши запросы и позиции (Вебмастер), поисковый трафик страницы темы (Метрика)."""
    from core.seo import apis, keywords, page, store
    today = dt.date.today()
    site = f"https://{config.SEO_SITE}"
    wm = store.get("wm_queries") or []
    if not wm and apis.has_yandex():
        try:
            wm = await apis.wm_queries(28) or []
        except Exception as e:
            log.warning("вебмастер: %s", e)
    org = {}
    try:
        end = today - dt.timedelta(days=1)
        org = {page.norm(u): v for u, v in (await apis.organic_by_page(end - dt.timedelta(days=27), end, 10000) or {}).items()}
    except Exception as e:
        log.warning("метрика: %s", e)
    md = [f"# Хабы: спрос и что у нас уже есть — {today:%d.%m.%Y}", "",
          "Вордстат — запросов за 30 дней (широкая частота). Вебмастер — запросы, по которым нас показывают (4 недели). "
          "Метрика — визиты из поиска на страницу темы за 28 дней.", ""]
    summary = []
    for slug, phrases in HUBS.items():
        url = page.norm(f"{site}/{slug}")
        rows = []
        for p in phrases:
            r = {"phrase": p}
            try:
                t = await wordstat.top(p, 200)
                r["total"] = t["total"]
                r["subs"] = "; ".join(f"{x['phrase']} {x['count']}" for x in t["results"][1:6])
                r.update(wordstat.trend(await wordstat.dynamics(p)))
            except Exception as e:
                r["error"] = str(e)[:100]
            rows.append(r)
        rows.sort(key=lambda r: -r.get("total", 0))
        stems = set().union(*(keywords.stems(p) - wordstat.GENERIC for p in phrases))
        ours = [q for q in wm if stems & keywords.stems(q["query"])]
        ours.sort(key=lambda q: -q["shows"])
        shows = sum(q["shows"] for q in ours)
        clicks = sum(q["clicks"] for q in ours)
        near = [q for q in ours if 3.5 < q["pos"] <= 30]
        demand = sum(r.get("total", 0) for r in rows)
        summary.append((slug, demand, len(ours), shows, clicks, len(near), org.get(url, 0)))
        md += [f"## /{slug}", "",
               f"Поисковый трафик страницы темы: {org.get(url, 0)} визитов за 28 дней. "
               f"Наших запросов в Вебмастере по теме: {len(ours)} (показов {_n(shows)}, кликов {_n(clicks)}, на 4–30 месте: {len(near)}).", "",
               "| Фраза | Частота | Год | 3 мес | Главные уточнения |", "|---|---|---|---|---|"]
        for r in rows:
            if "error" in r:
                md.append(f"| {r['phrase']} | ошибка: {r['error']} | | | |")
                continue
            yoy = f"{r['yoy']:+d}%" if "yoy" in r else "—"
            q = f"{r['q']:+d}%" if "q" in r else "—"
            md.append(f"| {r['phrase']} | {_n(r['total'])} | {yoy} | {q} | {r.get('subs', '')} |")
        if ours:
            md += ["", "Наши запросы (Вебмастер, топ-15 по показам):", ""]
            md += [f"- «{q['query']}» — поз. {q['pos']:.1f}, показов {q['shows']}, кликов {q['clicks']}" for q in ours[:15]]
        md.append("")
    md += ["## Сводка", "", "| Тема | Спрос (сумма фраз) | Наших запросов | Показов | Кликов | На 4–30 месте | Визитов на тему из поиска |",
           "|---|---|---|---|---|---|---|"]
    for s in sorted(summary, key=lambda x: -x[1]):
        md.append(f"| /{s[0]} | {_n(s[1])} | {s[2]} | {_n(s[3])} | {_n(s[4])} | {s[5]} | {s[6]} |")
    out = config.OUTBOX / "seo-plan" / f"hubs-{today:%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md), "utf-8")
    return str(out)


# ---------------- посещаемость тем (Метрика) ----------------
TOPIC_PATHS = ["/ui", "/ux", "/graphic-design", "/3d", "/career", "/typography", "/motion", "/product-design",
               "/branding", "/ai", "/mnenie", "/kreativ", "/bez-avtopilota", "/filter", "/education"]


async def _metrika(metrics: str, dims: str, d1: dt.date, d2: dt.date, filters: str = "") -> dict[str, float]:
    from core.seo import apis
    params = {"ids": config.METRIKA_COUNTER_ID, "metrics": metrics, "dimensions": dims,
              "date1": d1.isoformat(), "date2": d2.isoformat(), "limit": "100000", "accuracy": "full"}
    if filters:
        params["filters"] = filters
    data = await apis._get(apis.MK, params)
    out = {}
    for row in data.get("data", []):
        path = ((row.get("dimensions") or [{}])[0].get("name") or "").split("?")[0].rstrip("/") or "/"
        out[path] = out.get(path, 0) + float((row.get("metrics") or [0])[0])
    return out


async def topics_traffic() -> str:
    """Просмотры страниц тем, входы на них (все источники и поиск) — 28 дней к предыдущим 28."""
    from core.seo import apis
    if not (apis.has_yandex() and config.METRIKA_COUNTER_ID):
        return "Метрика не подключена: нужны YANDEX_OAUTH_TOKEN и METRIKA_COUNTER_ID."
    end = dt.date.today() - dt.timedelta(days=1)
    cur = (end - dt.timedelta(days=27), end)
    prev = (end - dt.timedelta(days=55), end - dt.timedelta(days=28))
    org = "ym:s:lastTrafficSource=='organic'"
    pv = await _metrika("ym:pv:pageviews", "ym:pv:URLPath", *cur)
    pv0 = await _metrika("ym:pv:pageviews", "ym:pv:URLPath", *prev)
    ent = await _metrika("ym:s:visits", "ym:s:startURLPath", *cur)
    so = await _metrika("ym:s:visits", "ym:s:startURLPath", *cur, filters=org)
    so0 = await _metrika("ym:s:visits", "ym:s:startURLPath", *prev, filters=org)
    site_pv = sum(pv.values()) or 1

    def ch(a, b):
        return f"{(a / b - 1) * 100:+.0f}%" if b else "—"
    L = [f"📊 Темы сайта, {cur[0]:%d.%m}–{cur[1]:%d.%m} (к прошлым 4 неделям)", "",
         "просмотры · входы на страницу · из них из поиска"]
    for p in sorted(TOPIC_PATHS, key=lambda x: -pv.get(x, 0)):
        a, b = pv.get(p, 0), pv0.get(p, 0)
        L.append(f"{p}: {int(a)} ({ch(a, b)}) · {int(ent.get(p, 0))} · {int(so.get(p, 0))} ({ch(so.get(p, 0), so0.get(p, 0))})")
    tot = sum(pv.get(p, 0) for p in TOPIC_PATHS if p not in ("/filter", "/education"))
    L += ["", f"Все 13 тем вместе — {tot / site_pv * 100:.1f}% просмотров сайта ({int(tot)} из {int(site_pv)})."]
    return "\n".join(L)
