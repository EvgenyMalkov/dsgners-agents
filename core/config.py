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
MONTHLY_LIMIT_USD = float(os.getenv("MONTHLY_LIMIT_USD", "50"))

MAX_STEPS = int(os.getenv("MAX_STEPS", "14"))

TEAM = ROOT / "team"
AGENTS = ROOT / "agents"
DATA = ROOT / "data"
OUTBOX = ROOT / "outbox"
for d in (DATA, OUTBOX, DATA / "feedback", DATA / "uploads"):
    d.mkdir(parents=True, exist_ok=True)
