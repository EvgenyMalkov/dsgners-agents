"""SQLite data/pr.db: что PR-агент знает между запусками."""
import json
import sqlite3
import time

from core import config

_db: sqlite3.Connection | None = None

HABR_HUBS = ["design", "ui", "web_design", "usability"]
DEFAULT_SOURCES = (
    [("habr", f"https://habr.com/ru/rss/hubs/{h}/articles/?fl=ru", f"Хабр · {h}") for h in HABR_HUBS]
    + [("vc", "https://vc.ru/rss/all", "vc.ru · вся лента (фильтр по теме)")]
)


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = sqlite3.connect(config.DATA / "pr.db", check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.executescript("""
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS runs(name TEXT PRIMARY KEY, ts REAL, ok INTEGER, note TEXT);
        CREATE TABLE IF NOT EXISTS sources(
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, url TEXT UNIQUE, title TEXT, enabled INTEGER DEFAULT 1,
            added REAL, last_ts REAL, last_ok INTEGER, last_count INTEGER, last_note TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS articles(
            id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, site TEXT, source_id INTEGER,
            title TEXT, author TEXT DEFAULT '', author_url TEXT DEFAULT '', published REAL, found REAL,
            stage TEXT DEFAULT 'new',          -- new | rejected | scored | sent | error
            reason TEXT DEFAULT '',            -- почему отсеяна
            chars INTEGER, images INTEGER, stats TEXT DEFAULT '{}', author_stats TEXT DEFAULT '{}',
            signal REAL, ai REAL, ai_json TEXT DEFAULT '{}', total REAL,
            sent REAL, vote INTEGER, vote_ts REAL);
        CREATE INDEX IF NOT EXISTS articles_stage ON articles(stage);
        """)
        if not _db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]:
            for kind, url, title in DEFAULT_SOURCES:
                add_source(kind, url, title)
        _db.commit()
    return _db


# --- ключ-значение и расписание
def get(key: str, default=None):
    r = db().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def put(key: str, value):
    db().execute("INSERT OR REPLACE INTO kv(key,value,ts) VALUES(?,?,?)",
                 (key, json.dumps(value, ensure_ascii=False), time.time()))
    db().commit()


def last_run(name: str) -> float:
    r = db().execute("SELECT ts FROM runs WHERE name=?", (name,)).fetchone()
    return r[0] if r else 0.0


def mark_run(name: str, ok: bool, note: str = ""):
    db().execute("INSERT OR REPLACE INTO runs(name,ts,ok,note) VALUES(?,?,?,?)", (name, time.time(), int(ok), note[:500]))
    db().commit()


def runs() -> dict:
    return {r["name"]: dict(r) for r in db().execute("SELECT * FROM runs")}


# --- источники
def add_source(kind: str, url: str, title: str = "") -> int:
    cur = db().execute("INSERT OR IGNORE INTO sources(kind,url,title,added) VALUES(?,?,?,?)",
                       (kind, url, title or url, time.time()))
    db().commit()
    return cur.lastrowid


def sources(only_enabled: bool = False) -> list[dict]:
    q = "SELECT * FROM sources" + (" WHERE enabled=1" if only_enabled else "") + " ORDER BY id"
    return [dict(r) for r in db().execute(q)]


def set_source(sid: int, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    db().execute(f"UPDATE sources SET {sets} WHERE id=?", (*fields.values(), sid))
    db().commit()


def del_source(sid: int) -> bool:
    n = db().execute("DELETE FROM sources WHERE id=?", (sid,)).rowcount
    db().commit()
    return bool(n)


# --- статьи
def known(url: str) -> bool:
    return db().execute("SELECT 1 FROM articles WHERE url=?", (url,)).fetchone() is not None


def add_article(url: str, site: str, source_id: int, title: str, author: str, published: float) -> int | None:
    cur = db().execute("INSERT OR IGNORE INTO articles(url,site,source_id,title,author,published,found) VALUES(?,?,?,?,?,?,?)",
                       (url, site, source_id, title, author, published, time.time()))
    db().commit()
    return cur.lastrowid if cur.rowcount else None


def update_article(aid: int, **fields):
    for k in ("stats", "author_stats", "ai_json"):
        if k in fields and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    sets = ", ".join(f"{k}=?" for k in fields)
    db().execute(f"UPDATE articles SET {sets} WHERE id=?", (*fields.values(), aid))
    db().commit()


def article(aid: int) -> dict | None:
    r = db().execute("SELECT * FROM articles WHERE id=?", (aid,)).fetchone()
    return _row(r) if r else None


def by_stage(stage: str, limit: int = 1000) -> list[dict]:
    return [_row(r) for r in db().execute("SELECT * FROM articles WHERE stage=? ORDER BY found LIMIT ?", (stage, limit))]


def digest_candidates(min_total: float, limit: int) -> list[dict]:
    return [_row(r) for r in db().execute(
        "SELECT * FROM articles WHERE stage='scored' AND total>=? ORDER BY total DESC LIMIT ?", (min_total, limit))]


def best_below(min_total: float) -> dict | None:
    r = db().execute("SELECT * FROM articles WHERE stage='scored' AND total<? AND found>? ORDER BY total DESC LIMIT 1",
                     (min_total, time.time() - 86400 * 2)).fetchone()
    return _row(r) if r else None


def voted(vote: int, limit: int = 8) -> list[dict]:
    return [_row(r) for r in db().execute(
        "SELECT * FROM articles WHERE vote=? ORDER BY vote_ts DESC LIMIT ?", (vote, limit))]


def counts_since(ts: float) -> dict:
    res = {"found": 0, "rejected": 0, "scored": 0, "sent": 0, "error": 0, "new": 0}
    for r in db().execute("SELECT stage, COUNT(*) n FROM articles WHERE found>=? GROUP BY stage", (ts,)):
        res[r["stage"]] = r["n"]
    res["found"] = sum(v for k, v in res.items() if k != "found")
    return res


def reasons_since(ts: float) -> list[tuple[str, int]]:
    return [(r["reason"], r["n"]) for r in db().execute(
        "SELECT reason, COUNT(*) n FROM articles WHERE stage='rejected' AND found>=? GROUP BY reason ORDER BY n DESC", (ts,))]


def votes_total() -> tuple[int, int]:
    up = db().execute("SELECT COUNT(*) FROM articles WHERE vote=1").fetchone()[0]
    down = db().execute("SELECT COUNT(*) FROM articles WHERE vote=-1").fetchone()[0]
    return up, down


def _row(r) -> dict:
    d = dict(r)
    for k in ("stats", "author_stats", "ai_json"):
        try:
            d[k] = json.loads(d.get(k) or "{}")
        except ValueError:
            d[k] = {}
    return d
