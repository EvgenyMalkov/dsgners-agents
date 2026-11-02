"""SQLite data/seo.db: что SEO-мониторинг знает о сайте между запусками."""
import json
import sqlite3
import time

from core import config

_db: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = sqlite3.connect(config.DATA / "seo.db", check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.executescript("""
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS urls(
            url TEXT PRIMARY KEY, first_seen REAL, lastmod TEXT, in_sitemap INTEGER DEFAULT 1,
            last_check REAL, status INTEGER, title TEXT DEFAULT '', h1 TEXT DEFAULT '',
            description TEXT DEFAULT '', issues TEXT DEFAULT '[]', optimized REAL);
        CREATE TABLE IF NOT EXISTS alerts(key TEXT PRIMARY KEY, active INTEGER, text TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS runs(name TEXT PRIMARY KEY, ts REAL, ok INTEGER, note TEXT);
        CREATE TABLE IF NOT EXISTS metrics(day TEXT, key TEXT, value REAL, PRIMARY KEY(day, key));
        """)
        _db.commit()
    return _db


# --- ключ-значение
def get(key: str, default=None):
    r = db().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def put(key: str, value):
    db().execute("INSERT OR REPLACE INTO kv(key,value,ts) VALUES(?,?,?)",
                 (key, json.dumps(value, ensure_ascii=False), time.time()))
    db().commit()


# --- расписание
def last_run(name: str) -> float:
    r = db().execute("SELECT ts FROM runs WHERE name=?", (name,)).fetchone()
    return r[0] if r else 0.0


def mark_run(name: str, ok: bool, note: str = ""):
    db().execute("INSERT OR REPLACE INTO runs(name,ts,ok,note) VALUES(?,?,?,?)", (name, time.time(), int(ok), note[:500]))
    db().commit()


def runs() -> list[dict]:
    return [dict(r) for r in db().execute("SELECT * FROM runs ORDER BY name")]


# --- алерты: сообщаем только о смене состояния, чтобы не спамить каждый час
def alert_state(key: str, active: bool, text: str = "") -> str | None:
    """Вернёт 'new' (проблема появилась), 'resolved' (ушла) или None (ничего не поменялось)."""
    r = db().execute("SELECT active FROM alerts WHERE key=?", (key,)).fetchone()
    was = bool(r and r[0])
    if active == was:
        return None
    db().execute("INSERT OR REPLACE INTO alerts(key,active,text,ts) VALUES(?,?,?,?)", (key, int(active), text, time.time()))
    db().commit()
    return "new" if active else "resolved"


def active_alerts() -> list[dict]:
    return [dict(r) for r in db().execute("SELECT * FROM alerts WHERE active=1 ORDER BY ts")]


# --- адреса сайта
def known_urls() -> dict[str, str]:
    return {r[0]: r[1] or "" for r in db().execute("SELECT url, lastmod FROM urls WHERE in_sitemap=1")}


def upsert_sitemap(urls: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """Обновить список адресов из sitemap. Вернёт (новые, изменённые, пропавшие)."""
    old = {r[0]: (r[1] or "", r[2]) for r in db().execute("SELECT url, lastmod, in_sitemap FROM urls")}
    now = time.time()
    new, changed = [], []
    for u, lm in urls.items():
        if u not in old:
            new.append(u)
            db().execute("INSERT INTO urls(url, first_seen, lastmod, in_sitemap) VALUES(?,?,?,1)", (u, now, lm))
        else:
            if lm and lm != old[u][0]:
                changed.append(u)
            db().execute("UPDATE urls SET lastmod=?, in_sitemap=1 WHERE url=?", (lm, u))
    gone = [u for u, (_, ins) in old.items() if ins and u not in urls]
    for u in gone:
        db().execute("UPDATE urls SET in_sitemap=0 WHERE url=?", (u,))
    db().commit()
    return new, changed, gone


def save_check(rep):
    db().execute("""INSERT INTO urls(url, first_seen, in_sitemap) VALUES(?,?,0) ON CONFLICT(url) DO NOTHING""",
                 (rep.url, time.time()))
    db().execute("UPDATE urls SET last_check=?, status=?, title=?, h1=?, description=?, issues=? WHERE url=?",
                 (time.time(), rep.status, rep.title, (rep.h1 or [""])[0], rep.description,
                  json.dumps(rep.issues, ensure_ascii=False), rep.url))
    db().commit()


def mark_optimized(url: str):
    db().execute("UPDATE urls SET optimized=? WHERE url=?", (time.time(), url))
    db().commit()


def was_optimized(url: str) -> bool:
    r = db().execute("SELECT optimized FROM urls WHERE url=?", (url,)).fetchone()
    return bool(r and r[0])


def pages_with_titles(limit=5000) -> list[dict]:
    return [dict(r) for r in db().execute(
        "SELECT url, title, h1 FROM urls WHERE in_sitemap=1 AND status=200 AND title!='' ORDER BY first_seen DESC LIMIT ?",
        (limit,))]


# --- метрики по дням (для сравнения неделя к неделе)
def put_metric(day: str, key: str, value: float):
    db().execute("INSERT OR REPLACE INTO metrics(day,key,value) VALUES(?,?,?)", (day, key, value))
    db().commit()


def metric(day: str, key: str):
    r = db().execute("SELECT value FROM metrics WHERE day=? AND key=?", (day, key)).fetchone()
    return r[0] if r else None
