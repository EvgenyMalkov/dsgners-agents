"""Проверка версии перед запуском (без API): код импортируется, офис собирается, рендер работает.
Запуск: .venv/bin/python -m core.selftest — код выхода 0, если всё хорошо."""
import asyncio
import importlib
import sys

from core import config


async def main() -> int:
    import bot  # noqa: F401 — бот импортируется без ошибок
    from core.agent import build_system
    from core.office import AGENTS
    from core.render import render
    from core.web import make_app

    make_app()
    from core.seo import monitor, page, scheduler  # noqa: F401 — SEO-мониторинг импортируется
    rep = page.analyze(page.Fetched(url="https://x.ru/a", final="https://x.ru/a", status=200,
                                    html="<html lang=ru><head><title>Тест</title></head><body><h1>Т</h1></body></html>"),
                       is_article=True)
    assert any(c == "no_description" for _, c, _ in rep.issues), "проверка страниц сломана"
    assert scheduler.JOBS and monitor.status_text()
    for a, meta in AGENTS.items():
        if meta["active"]:
            tools = importlib.import_module(f"agents.{a}.tools")
            assert tools.TOOLS, f"у агента {a} нет инструментов"
            build_system(a, getattr(tools, "system_extra", lambda: "")())
    out = config.OUTBOX / "selftest" / "tg-post.png"
    issues = await render("tg-post", {"rubric": "Тест", "title": "Проверка версии", "theme": "dark"}, out)
    assert out.exists() and out.stat().st_size > 5000, "рендер не создал картинку"
    print("Проверка пройдена. Рендер:", out, "| замечания вёрстки:", issues or "нет")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as e:
        print("ПРОВЕРКА НЕ ПРОЙДЕНА:", repr(e))
        sys.exit(1)
