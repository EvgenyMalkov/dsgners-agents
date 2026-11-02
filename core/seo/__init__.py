"""SEO-мониторинг dsgners: проверки страниц, обход сайта, Вебмастер/Метрика/IndexNow, расписание.

page.py      — скачать страницу и проверить SEO (title, description, H1, canonical, noindex, alt, OG, разметка)
store.py     — SQLite data/seo.db: известные URL, снимки robots.txt, состояния алертов
apis.py      — Яндекс Вебмастер, Яндекс Метрика, IndexNow (работают, когда в .env есть токены)
monitor.py   — что проверяем каждый час / 30 минут / день / неделю
scheduler.py — запуск по расписанию внутри процесса бота
"""
