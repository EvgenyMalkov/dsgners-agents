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
    from core.pr import jobs as pr_jobs, score as pr_score, sources as pr_sources  # noqa: F401 — PR-агент
    items = pr_sources.parse_rss('<rss><channel><item><title>T</title><link>https://habr.com/ru/articles/123456/</link>'
                                 '<pubDate>Wed, 04 Nov 2026 10:00:00 +0000</pubDate></item></channel></rss>')
    assert items and items[0].url.endswith("/123456/"), "разбор RSS сломан"
    art = pr_sources.Article(url="https://vc.ru/x/1", site="vc", title="Кейс", text="дизайн " * 2000, images=0)
    assert pr_score.filter_reason(art, "vc") == "" and pr_jobs.status(), "фильтр PR сломан"
    from core.direct import api as dr_api, jobs as dr_jobs  # noqa: F401 — Директ-агент
    rows = dr_api.parse_tsv("Date\tCampaignId\tPlacement\tClicks\tCost\tSessions\tBounces\n"
                            "2026-11-01\t7\tgame.app\t40\t120.5\t38\t35\n2026-11-01\t7\tgood.ru\t40\t100\t39\t5\n")
    assert rows and rows[0]["CampaignId"] == 7 and rows[0]["Cost"] == 120.5, "разбор отчёта Директа сломан"
    bad = dr_jobs.junk([{"placement": r["Placement"], "clicks": r["Clicks"], "cost": r["Cost"],
                         "sessions": r["Sessions"], "bounces": r["Bounces"]} for r in rows])
    assert [b["placement"] for b in bad] == ["game.app"], "поиск мусорных площадок сломан"
    assert dr_jobs.status(), "статус Директа сломан"
    for a, meta in AGENTS.items():
        if meta["active"]:
            tools = importlib.import_module(f"agents.{a}.tools")
            assert tools.TOOLS, f"у агента {a} нет инструментов"
            build_system(a, getattr(tools, "system_extra", lambda: "")())
    out = config.OUTBOX / "selftest" / "tg-post.png"
    issues = await render("tg-post", {"rubric": "Тест", "title": "Проверка версии", "palette": "auto"}, out)
    assert out.exists() and out.stat().st_size > 5000, "рендер не создал картинку"
    print("Проверка пройдена. Рендер:", out, "| замечания вёрстки:", issues or "нет")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as e:
        print("ПРОВЕРКА НЕ ПРОЙДЕНА:", repr(e))
        sys.exit(1)
