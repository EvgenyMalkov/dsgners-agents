"""Учёт расходов на API: data/spend.jsonl, одна строка на запрос."""
import datetime as dt
import json

from core import config

LOG = config.DATA / "spend.jsonl"


def record(agent: str, usage) -> float:
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    stu = getattr(usage, "server_tool_use", None)
    searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
    usd = (inp * config.PRICE_IN + cw * config.PRICE_IN * 1.25 + cr * config.PRICE_IN * 0.1
           + out * config.PRICE_OUT) / 1_000_000 + searches * 0.01   # поиск — $10 за 1000
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": dt.datetime.now().isoformat(timespec="seconds"), "agent": agent,
                            "in": inp, "out": out, "cache_w": cw, "cache_r": cr, "usd": round(usd, 5)}) + "\n")
    return usd


def this_month() -> float:
    if not LOG.exists():
        return 0.0
    month = dt.date.today().strftime("%Y-%m")
    total = 0.0
    for line in LOG.read_text("utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row["ts"].startswith(month):
            total += row["usd"]
    return total


def by_agent_this_month() -> dict:
    res: dict = {}
    if not LOG.exists():
        return res
    month = dt.date.today().strftime("%Y-%m")
    for line in LOG.read_text("utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row["ts"].startswith(month):
            res[row["agent"]] = res.get(row["agent"], 0) + row["usd"]
    return res
