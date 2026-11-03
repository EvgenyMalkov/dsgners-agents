"""Настройки берутся из .env в корне репозитория."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = os.getenv("MODEL", "claude-sonnet-5-5")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x}

# Цены модели за 1 млн токенов, $ — для подсчёта расходов. Сверяй с docs.claude.com при смене модели.
PRICE_IN = float(os.getenv("PRICE_IN", "2"))
PRICE_OUT = float(os.getenv("PRICE_OUT", "10"))
# Сильная модель — для задач, где качество окупается: контент-план, текст статьи, месячный разбор
MODEL_SMART = os.getenv("MODEL_SMART", "claude-opus-5-5")
PRICE_IN_SMART = float(os.getenv("PRICE_IN_SMART", "4"))
PRICE_OUT_SMART = float(os.getenv("PRICE_OUT_SMART", "20"))
MONTHLY_LIMIT_USD = float(os.getenv("MONTHLY_LIMIT_USD", "50"))

MAX_STEPS = int(os.getenv("MAX_STEPS", "14"))
WEB_SEARCH = os.getenv("WEB_SEARCH", "1") == "1"   # поиск в интернете для SEO-агента (конкуренты, факты)

# ИИ-офис (веб)
OFFICE_TOKEN = os.getenv("OFFICE_TOKEN", "")
OFFICE_HOST = os.getenv("OFFICE_HOST", "127.0.0.1")
OFFICE_PORT = int(os.getenv("OFFICE_PORT", "8080"))

# SEO-мониторинг (core/seo)
SEO_MONITOR = os.getenv("SEO_MONITOR", "1") == "1"
SEO_SITE = os.getenv("SEO_SITE", "dsgners.ru").strip().lower().removeprefix("https://").strip("/")
# какие адреса считать статьями/новостями/списками (регулярка по URL); пусто — все, кроме служебных
# на dsgners статьи выглядят так: /<автор>/<номер>-<адрес>, например /na_produkte/12366-dizayn-marafon-a-ne-sprint
SEO_CONTENT_PATTERN = os.getenv("SEO_CONTENT_PATTERN", "") or r"^https?://[^/]+/[^/]+/\d+-[^/]+$"
# какие адреса не проверять вообще (профили, поиск, служебные)
SEO_IGNORE_PATTERN = os.getenv("SEO_IGNORE_PATTERN", r"/(login|auth|signup|register|search|settings|cart|api)(/|$)|\?")
SEO_CRAWL_RPS = float(os.getenv("SEO_CRAWL_RPS", "1.5"))          # запросов в секунду при обходе
SEO_CRAWL_MAX = int(os.getenv("SEO_CRAWL_MAX", "5000"))          # максимум страниц за недельный обход
SEO_AUTO_OPTIMIZE_PER_DAY = int(os.getenv("SEO_AUTO_OPTIMIZE_PER_DAY", "10"))  # авто-оформление новых статей
SEO_TOPICS_PER_WEEK = int(os.getenv("SEO_TOPICS_PER_WEEK", "5"))   # тем в недельном контент-плане
# 1 — планы статей из контент-плана принимаются автоматически (ты согласуешь только список тем и готовый текст)
SEO_AUTO_PLANS = os.getenv("SEO_AUTO_PLANS", "0") == "1"
SEO_TZ = os.getenv("SEO_TZ", "Europe/Moscow")
YANDEX_OAUTH_TOKEN = os.getenv("YANDEX_OAUTH_TOKEN", "")
METRIKA_COUNTER_ID = os.getenv("METRIKA_COUNTER_ID", "")
INDEXNOW_KEY = os.getenv("INDEXNOW_KEY", "")

TEAM = ROOT / "team"
AGENTS = ROOT / "agents"
DATA = ROOT / "data"
OUTBOX = ROOT / "outbox"
for d in (DATA, OUTBOX, DATA / "feedback", DATA / "uploads", DATA / "agents", DATA / "knowledge"):
    d.mkdir(parents=True, exist_ok=True)
