"""Демо-данные для превью шаблонов во вкладке «Шаблоны» офиса. Картинки — нейтральные заглушки из team/assets/samples."""
import asyncio
import hashlib
from pathlib import Path

from core import config

S = "team/assets/samples/"
PEOPLE = [S + f"person-{i}.png" for i in range(1, 6)]
LOGOS = [S + f"logo-{i}.png" for i in range(1, 4)]

SAMPLES = {
    "tg-post": {"title": "Дизайн, который дарит счастье", "rubric": "Статья", "author": "Имя Автора",
                "palette": "green", "motifs": ["zigzag", "bubble"]},
    "square": {"title": "Как собрать портфолио, которое читают", "rubric": "Гайд", "palette": "blue",
               "motifs": ["cursor", "cloud", "star8"], "layout": "info-top"},
    "story": {"title": "Новая рубрика по пятницам", "subtitle": "Разборы работ подписчиков", "date": "15.05 в 20:00",
              "palette": "lime", "motifs": ["cloud", "zigzag", "squiggle"]},
    "site-banner": {"title": "Пиши о дизайне — тебя прочитают", "rubric": "Авторам", "palette": "pink",
                    "motifs": ["heart", "star8"], "hide_logo": True},
    "article-cover": {"title": "Как общаться с клиентами", "rubric": "Гайд", "author": "Имя Автора", "palette": "cyan",
                      "motifs": ["bubble", "squiggle"]},
    "article-steps": {"title": "5 ошибок в портфолио", "rubric": "Чек-лист", "palette": "orange", "motifs": ["heart", "zigzag"],
                      "items": ["Нет контекста задачи", "40 работ вместо 8", "Только картинки без процесса",
                                "Нет результата", "Старые проекты в начале"]},
    "article-compare": {"title": "Три редактора интерфейсов", "rubric": "Сравнение", "palette": "violet", "motifs": ["cloud"],
                        "table": {"columns": ["", "Сервис А", "Сервис Б", "Сервис В"],
                                  "rows": [["Цена", "от 15 $", "бесплатно", "от 10 $"], ["Браузер", "да", "да", "нет"],
                                           ["Совместная работа", "да", "да", "частично"]]}},
    "showcase": {"image": S + "ui-desktop.png", "palette": "lime"},
    "showcase-phones": {"images": [S + "ui-mobile-1.png", S + "ui-mobile-2.png", S + "ui-mobile-3.png"], "palette": "violet"},
    "collage": {"title": "Наши любимые спикеры", "subtitle": "Сегодня в прямом эфире", "date": "15.05 в 20:00",
                "graphic": "bars", "images": PEOPLE[:4]},
    "digest": {"title": "Дайджест новостей", "subtitle": "события с 21 по 30 мая", "images": [PEOPLE[0], LOGOS[2], PEOPLE[2]]},
    "poster": {"title": "Рейтинг дизайнерс", "subtitle": "Самые полезные авторы по мнению самих авторов",
               "badges": [{"image": LOGOS[0], "label": "Топ 2"}, {"image": LOGOS[1], "label": "Топ 1"},
                          {"image": PEOPLE[3], "label": "Топ 3"}]},
    "promo": {"title": "Аудитория вас найдёт", "images": [S + "ui-mobile-1.png", S + "ui-mobile-2.png"]},
    "promo-duo": {"title": "Блог-платформа для дизайнеров", "image": S + "ui-mobile-3.png"},
}

_lock = asyncio.Lock()


def _version() -> str:
    """Меняется при любой правке шаблонов/движка рендера — тогда превью перерисуются."""
    files = [config.AGENTS / "designer" / "templates" / "index.json", config.ROOT / "core" / "render.py",
             config.ROOT / "core" / "layouts.py", config.ROOT / "core" / "brandgfx.py", config.TEAM / "assets" / "shapes.json",
             config.TEAM / "design-system.json", *sorted((config.AGENTS / "designer" / "templates").glob("*.html"))]
    h = hashlib.md5()
    for f in files:
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:10]


async def preview(name: str) -> Path:
    from core.render import render, templates_index
    if name not in templates_index() or name.startswith("_"):
        raise KeyError(name)
    out = config.DATA / "previews" / f"{name}-{_version()}.png"
    if out.exists():
        return out
    async with _lock:
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            for old in out.parent.glob(f"{name}-*.png"):
                old.unlink()
            data = dict(SAMPLES.get(name, {"title": "Пример заголовка"}))
            await render(name, data, out)
            for extra in (out.with_suffix(".html"),):
                if extra.exists():
                    extra.unlink()
    return out
