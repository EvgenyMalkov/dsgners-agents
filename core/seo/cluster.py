"""Каркас кластера направления на данных: подтемы → намерения запросов → наши статьи → что уже работает.

По каждой подтеме направления (inventory.T):
- Вордстат: топ-300 уточнений по 1–2 главным фразам, разложенных по намерениям (примеры, что это, как сделать,
  цена, нейросети, шаблоны, тренды, обучение, заказать, конкретные бренды) с суммой частот и примерами фраз;
  похожие запросы, которые не относятся ни к одной подтеме, — кандидаты в новые подтемы;
- наши статьи подтемы, разложенные по тем же намерениям по заголовку;
- Вебмастер: наши запросы по подтеме (позиция, показы, клики);
- Метрика: какие статьи подтемы уже приводят людей из поиска (28 дней).
Запуск: /cluster branding (или другое направление). Отчёт — markdown в outbox/seo-plan.
"""
import datetime as dt
import logging
import re

from core import config
from core.seo import apis, inventory, keywords, page, store, wordstat

log = logging.getLogger("seo.cluster")

INTENTS = [  # порядок важен: первое совпадение определяет намерение
    ("Цена", r"цен|стоимост|сколько стоит|прайс|недорог|бюджет"),
    ("Нейросети и генераторы", r"нейросет|(^| )ии( |$)|(^| )ai( |$)|генератор|midjourney|chatgpt|сгенерир"),
    ("Шаблоны и скачать", r"шаблон|скачать|pdf|мокап|mockup|бесплатн|psd|figma|фигм|готов"),
    ("Тренды", r"тренд|202[4-9]|современн|модн|актуальн"),
    ("Обучение", r"курс|обучени|научиться|урок|книг|школ"),
    ("Заказать и студии", r"заказать|студи|агентств|компани|услуг|москв|спб|петербург|екатеринбург|казан|новосибирск"),
    ("Примеры и подборки", r"пример|образ(е|ц)|лучш|топ|красив|референс|подборк|галере|идеи"),
    ("Что это и виды", r"(^| )это( |$)|что такое|простыми|значит|определени|виды|типы|элемент|состав|из чего|отлич|разниц|функци|зачем"),
    ("Как сделать", r"как (сделать|создать|придумать|нарисовать|разработать|составить|оформить|собрать)|создани|разработк|этап|бриф|самому|своими|пошагов|инструкц|процесс"),
]
SEEDS = {
    "Айдентика": ["айдентика"], "Логотип": ["логотип", "разработка логотипа"], "Фирменный стиль": ["фирменный стиль"],
    "Брендбук и гайдлайны": ["брендбук", "гайдлайн"], "Упаковка": ["дизайн упаковки"], "Нейминг": ["нейминг"],
    "Ребрендинг": ["ребрендинг"], "Брендинг (общее)": ["брендинг"],
}


def intent_of(phrase: str) -> str:
    p = " " + re.sub(r"[^a-zа-яё0-9]+", " ", phrase.lower().replace("ё", "е")) + " "
    for name, rx in INTENTS:
        if re.search(rx, p):
            return name
    return "Конкретные бренды и прочее"


def _n(x) -> str:
    return f"{int(x):,}".replace(",", " ")


async def build(direction: str = "Брендинг") -> str:
    subs = inventory.T.get(direction)
    if not subs:
        raise ValueError(f"Нет направления «{direction}». Есть: {', '.join(inventory.T)}")
    posts = inventory._posts()
    meta = {r["url"]: (r["title"] or "") for r in store.db().execute("SELECT url, title FROM urls")}
    try:
        from core.seo import classify
        llm = classify.results()
    except Exception:
        llm = {}
    wm = store.get("wm_queries") or []
    org = {}
    try:
        end = dt.date.today() - dt.timedelta(days=1)
        org = {page.norm(u): v for u, v in (await apis.organic_by_page(end - dt.timedelta(days=27), end, 20000) or {}).items()}
    except Exception as e:
        log.warning("метрика: %s", e)
    all_sub_rx = [re.compile(pat) for _, pat in subs.values()]
    today = dt.date.today()
    md = [f"# Кластер «{direction}»: данные для каркаса — {today:%d.%m.%Y}", "",
          "Вордстат — топ уточнений за 30 дней (широкая частота), разложены по намерению по словам в запросе. "
          "Наши статьи — посты подтемы (поиск по словам + раскладка моделью), разложены по тем же намерениям по заголовку. "
          "Метрика — визиты из поиска за 28 дней.", ""]
    summary = []
    stray: dict[str, int] = {}
    for name, (phrase, pat) in subs.items():
        rx = re.compile(pat)
        lab = f"{direction} / {name}"
        mine = [p for p in posts if rx.search(p["text"]) or lab in llm.get(p["url"], ())]
        # Вордстат
        groups: dict[str, list] = {}
        total = 0
        for seed in SEEDS.get(name, [phrase]):
            if not wordstat.enabled():
                break
            try:
                t = await wordstat.top(seed, 300)
            except Exception as e:
                md.append(f"_Вордстат «{seed}»: {e}_")
                continue
            total = max(total, t["total"])
            for x in t["results"]:
                if x["phrase"].strip().lower() == seed:
                    continue          # сама главная фраза — это и есть «спрос по главной фразе»
                groups.setdefault(intent_of(x["phrase"]), []).append(x)
            for a in t["associations"]:
                a_text = "-" + re.sub(r"[^a-zа-яё0-9]+", "-", a["phrase"].lower()) + "-"
                if not any(r.search(a_text) for r in all_sub_rx):
                    stray[a["phrase"]] = max(stray.get(a["phrase"], 0), a["count"])
        # наши статьи по намерениям
        art: dict[str, list] = {}
        for p in mine:
            title = meta.get(p["url"]) or p["title"]
            art.setdefault(intent_of(title), []).append((title, p["url"]))
        visits = sorted(((org.get(page.norm(p["url"]), 0), meta.get(p["url"]) or p["title"], p["url"]) for p in mine), reverse=True)
        stems = keywords.stems(phrase) - wordstat.GENERIC
        ours = sorted((q for q in wm if stems & keywords.stems(q["query"])), key=lambda q: -q["shows"])
        summary.append((name, total, len(mine), sum(v for v, _, _ in visits), len(ours)))
        md += [f"## {name}", "",
               f"Спрос по главной фразе: {_n(total)} в месяц · наших статей: {len(mine)} · "
               f"визитов из поиска на них за 28 дней: {_n(sum(v for v, _, _ in visits))} · наших запросов в Вебмастере: {len(ours)}", "",
               "| Намерение | Спрос (сумма уточнений) | Главные запросы | Наших статей | Примеры наших статей |",
               "|---|---|---|---|---|"]
        names = [n for n, _ in INTENTS] + ["Конкретные бренды и прочее"]
        for intent in names:
            g = sorted(groups.get(intent, []), key=lambda x: -x["count"])
            # дубли фраз из двух семян
            seen, uniq = set(), []
            for x in g:
                if x["phrase"] not in seen:
                    seen.add(x["phrase"])
                    uniq.append(x)
            a = art.get(intent, [])
            if not uniq and not a:
                continue
            top = "; ".join(f"{x['phrase']} {x['count']}" for x in uniq[:5])
            ex = "; ".join(t for t, _ in a[:3])
            md.append(f"| {intent} | {_n(sum(x['count'] for x in uniq))} | {top} | {len(a)} | {ex} |")
        if any(v for v, _, _ in visits):
            md += ["", "Что уже приводит людей из поиска:", ""]
            md += [f"- {v} визитов — {t} — {u.replace('https://' + config.SEO_SITE, '')}" for v, t, u in visits[:7] if v]
        if ours:
            md += ["", "Наши запросы в Вебмастере:", ""]
            md += [f"- «{q['query']}» — поз. {q['pos']:.1f}, показов {q['shows']}, кликов {q['clicks']}" for q in ours[:10]]
        md.append("")
    md += ["## Сводка по подтемам", "", "| Подтема | Спрос | Наших статей | Визитов из поиска | Запросов в Вебмастере |",
           "|---|---|---|---|---|"]
    md += [f"| {n} | {_n(t)} | {c} | {_n(v)} | {q} |" for n, t, c, v, q in sorted(summary, key=lambda s: -s[1])]
    if stray:
        md += ["", "## Похожие запросы вне подтем — кандидаты в новые подтемы", ""]
        md += [f"- {p} — {_n(c)}" for p, c in sorted(stray.items(), key=lambda kv: -kv[1])[:25]]
    out = config.OUTBOX / "seo-plan" / f"cluster-{re.sub(r'[^a-zа-я0-9]+', '-', direction.lower())}-{today:%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md), "utf-8")
    return str(out)
