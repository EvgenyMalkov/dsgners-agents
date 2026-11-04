"""Инвентаризация базы под хабы: сколько постов у нас уже есть по каждой подтеме направления.

Источник — адреса постов из sitemap (sitemap-posts-*.xml в data/seo.db): в адресе транслитом зашит заголовок
(`/автор/159715-kak-perestroila-rabotu-dizayn-komandyi-s-pomoschyu-ii`), а у проверенных страниц есть и title.
По каждой подтеме: число постов, спрос в Вордстате (если подключён) и примеры адресов.
Запуск: /hub_inventory в боте. Без ИИ, бесплатно (кроме запросов к Вордстату, они кешируются).
"""
import datetime as dt
import logging
import re

from core import config
from core.seo import store, wordstat

log = logging.getLogger("seo.inventory")

# направление → подтема → (фраза для Вордстата, шаблон по адресу-транслиту и заголовку)
T = {
    "Брендинг": {
        "Айдентика": ("айдентика", r"identik|aydentik|айдентик"),
        "Логотип": ("разработка логотипа", r"logotip|логотип"),
        "Фирменный стиль": ("фирменный стиль", r"firmenn|фирменн"),
        "Брендбук и гайдлайны": ("брендбук", r"brendbuk|brandbook|gaydlayn|брендбук|гайдлайн"),
        "Упаковка": ("дизайн упаковки", r"upakovk|упаковк|packaging"),
        "Нейминг": ("нейминг", r"neyming|нейминг|naming"),
        "Ребрендинг": ("ребрендинг", r"rebrending|ребрендинг"),
        "Брендинг (общее)": ("брендинг", r"brending|брендинг|branding"),
    },
    "UI": {
        "UI kit и дизайн-системы": ("ui kit", r"ui-?kit|dizayn-sistem|design-system|дизайн-систем"),
        "Мобильные приложения": ("дизайн мобильного приложения", r"prilozheni|приложени|mobiln"),
        "Сайты и лендинги": ("дизайн сайта", r"-sayt|lending|landing|сайт|лендинг"),
        "Интерфейсы (общее)": ("дизайн интерфейсов", r"interfeys|интерфейс|-ui-"),
        "Figma": ("figma", r"figm|фигм"),
        "Онбординг": ("онбординг", r"onbording|онбординг"),
    },
    "UX": {
        "Исследования": ("ux исследования", r"issledovan|интервью|intervyu|исследован|research"),
        "Юзабилити и тесты": ("юзабилити", r"yuzabiliti|usability|юзабилити|testirovani"),
        "CJM и сценарии": ("cjm", r"-cjm|customer-journey|(st|s)senari|scenari|сценари"),
        "UX (общее)": ("ux дизайн", r"-ux-"),
    },
    "Типографика": {
        "Шрифты": ("шрифты для дизайна", r"shrift|шрифт|-font"),
        "Типографика": ("типографика", r"tipografik|типографик"),
        "Леттеринг и каллиграфия": ("леттеринг", r"lettering|kalligraf|леттеринг|каллиграф"),
    },
    "Графический дизайн": {
        "Плакаты и постеры": ("дизайн плаката", r"plakat|poster|плакат|постер|afish"),
        "Иллюстрация": ("иллюстрация", r"illyustra(c|ts)|иллюстрац"),
        "Цвет и палитры": ("сочетание цветов", r"-(c|ts)vet|palitr|цвет|палитр|color"),
        "Композиция и сетки": ("модульная сетка", r"kompozi(c|ts)|-setk|композиц|сетк"),
        "Презентации": ("дизайн презентации", r"prezenta(c|ts)|презентац"),
    },
    "Карьера": {
        "Портфолио": ("портфолио дизайнера", r"portfolio|портфолио"),
        "Резюме и собеседования": ("резюме дизайнера", r"rezyume|sobesedovan|резюме|собеседован"),
        "Фриланс и цены": ("фриланс дизайнер", r"frilans|stoimost|skolko-stoit|(c|ts)enoobraz|фриланс|стоимост|сколько-стоит"),
        "Поиск работы и вакансии": ("вакансии дизайнер", r"vakansi|poisk-rabot|trudoustr|ваканси|поиск-работы"),
        "Выгорание и рост": ("выгорание дизайнера", r"vygoran|vygoreni|выгоран|grejd|грейд|sinyor|джун|dzhun"),
    },
    "Нейросети": {
        "Нейросети в дизайне": ("нейросети для дизайна", r"neyroset|нейросет|-ii-|-ai-|midjourney|chatgpt|claude|vaybkod|вайбкод"),
    },
    "Моушн": {"Моушн и анимация": ("моушн дизайн", r"moushn|motion|anima(c|ts)|моушн|анимац")},
    "3D": {"3D": ("3d дизайн", r"-3d|blender|блендер|cinema-4d|c4d")},
    "Продуктовый дизайн": {"Продуктовый дизайн": ("продуктовый дизайн", r"produktov|продуктов|product-design")},
}


def _posts() -> list[dict]:
    d = store.db()
    rows = d.execute("SELECT url, title, h1, sm FROM urls WHERE in_sitemap=1").fetchall()
    out = []
    for r in rows:
        url, sm = r["url"], (r["sm"] or "")
        if sm and "posts" not in sm:
            continue
        if not sm and not re.search(r"/[^/]+/\d+-", url):
            continue
        slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
        text = f"{slug} {(r['title'] or '').lower()} {(r['h1'] or '').lower()}"
        text = "-" + re.sub(r"[^a-zа-яё0-9]+", "-", text.replace("ё", "е")) + "-"
        out.append({"url": url, "text": text,
                    "title": r["title"] or r["h1"] or slug})
    return out


async def build() -> str:
    posts = _posts()
    today = dt.date.today()
    md = [f"# Инвентаризация базы под хабы — {today:%d.%m.%Y}", "",
          f"Постов в sitemap: {len(posts)}. Подтема определяется по заголовку в адресе (транслит) и по title, "
          "если страница уже проверялась. Один пост может попасть в несколько подтем.", ""]
    hit_any = set()
    summary = []
    try:
        from core.seo import classify
        llm = classify.results()
    except Exception as e:
        log.warning("classify: %s", e)
        llm = {}
    if llm:
        md.insert(3, f"Плюс раскладка моделью по заголовку и описанию: разобрано {len(llm)} постов (/hub_classify).")
    for direction, subs in T.items():
        md += [f"## {direction}", "", "| Подтема | Постов у нас | Спрос, в месяц | Примеры |", "|---|---|---|---|"]
        for name, (phrase, pat) in subs.items():
            rx = re.compile(pat)
            lab = f"{direction} / {name}"
            found = [p for p in posts if rx.search(p["text"]) or lab in llm.get(p["url"], ())]
            hit_any.update(p["url"] for p in found)
            demand = ""
            if wordstat.enabled():
                try:
                    demand = (await wordstat.top(phrase, 1, save=False))["total"]
                except Exception as e:
                    log.warning("%s: %s", phrase, e)
            ex = "; ".join(p["url"].replace(f"https://{config.SEO_SITE}", "") for p in found[:4])
            md.append(f"| {name} ({phrase}) | {len(found)} | {demand if demand != '' else '—'} | {ex} |")
            summary.append((direction, name, len(found), demand or 0))
        md.append("")
    md += [f"Не попали ни в одну подтему: {len(posts) - len(hit_any)} из {len(posts)} постов "
           "(новости, мнения, интервью, кейсы без ключевых слов в заголовке).", "",
           "## Где больше всего материала (постов ≥ 15), по спросу", "",
           "| Направление | Подтема | Постов | Спрос |", "|---|---|---|---|"]
    for d_, n, c, dem in sorted((s for s in summary if s[2] >= 15), key=lambda s: -s[3]):
        md.append(f"| {d_} | {n} | {c} | {dem} |")
    out = config.OUTBOX / "seo-plan" / f"hub-inventory-{today:%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md), "utf-8")
    return str(out)
