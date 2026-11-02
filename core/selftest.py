"""Проверка без API: рендерит тестовый визуал. Запуск: .venv/bin/python -m core.selftest"""
import asyncio

from core import config
from core.render import render


async def main():
    out = config.OUTBOX / "selftest" / "tg-post.png"
    issues = await render("tg-post", {"rubric": "Тест", "title": "Дизайнер dsgners на связи", "theme": "dark"}, out)
    print("Рендер OK:", out, "| проблемы:", issues or "нет")


if __name__ == "__main__":
    asyncio.run(main())
