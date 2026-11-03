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
        cols = {r[1] for r in _db.execute("PRAGMA table_info(urls)")}
        if "sm" not in cols:          # из какого sitemap-файла адрес
            _db.execute("ALTER TABLE urls ADD COLUMN sm TEXT DEFAULT ''")
        if "missing" not in cols:     # сколько успешных прочтений подряд адреса нет в sitemap
            _db.execute("ALTER TABLE urls ADD COLUMN missing INTEGER DEFAULT 0")
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


def upsert_sitemap(urls: dict, failed_files: list[str] = (), confirm_runs: int = 2):
    """Обновить список адресов из sitemap.

    urls — {адрес: (lastmod, sitemap-файл)}. Адреса из sitemap-файлов, которые сейчас не загрузились (failed_files),
    пропавшими не считаем. Пропавшим адрес становится, только если его нет confirm_runs успешных прочтений подряд —
    так разовый сбой сайта или сети не даёт ложной тревоги.
    Вернёт (новые, изменённые, пропавшие).
    """
    old = {r[0]: (r[1] or "", r[2], r[3] or "", r[4] or 0)
           for r in db().execute("SELECT url, lastmod, in_sitemap, sm, missing FROM urls")}
    failed = set(failed_files)
    now = time.time()
    new, changed = [], []
    for u, (lm, sm) in urls.items():
        if u not in old:
            new.append(u)
            db().execute("INSERT INTO urls(url, first_seen, lastmod, in_sitemap, sm, missing) VALUES(?,?,?,1,?,0)",
                         (u, now, lm, sm))
        else:
            if lm and lm != old[u][0] and old[u][1]:
                changed.append(u)
            db().execute("UPDATE urls SET lastmod=?, in_sitemap=1, sm=?, missing=0 WHERE url=?", (lm, sm, u))
    gone = []
    for u, (_, ins, sm, miss) in old.items():
        if not ins or u in urls or (sm and sm in failed):
            continue
        if failed and not sm:
            continue        # старый адрес без известного источника — при частичном сбое не трогаем
        if miss + 1 >= confirm_runs:
            gone.append(u)
            db().execute("UPDATE urls SET in_sitemap=0, missing=? WHERE url=?", (miss + 1, u))
        else:
            db().execute("UPDATE urls SET missing=? WHERE url=?", (miss + 1, u))
    db().commit()
    return new, changed, gone


def urls_from_files(files: list[str]) -> dict[str, str]:
    """Последние известные адреса из указанных sitemap-файлов (подставляем, когда файл не загрузился)."""
    if not files:
        return {}
    q = ",".join("?" * len(files))
    return {r[0]: r[1] or "" for r in db().execute(
        f"SELECT url, lastmod FROM urls WHERE in_sitemap=1 AND sm IN ({q})", list(files))}


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
