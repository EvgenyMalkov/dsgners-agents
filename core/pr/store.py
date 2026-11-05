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
        -- авторы дизайнерс: профиль, имя, внешние ссылки из профиля
        CREATE TABLE IF NOT EXISTS dsg_authors(slug TEXT PRIMARY KEY, name TEXT DEFAULT '', links TEXT DEFAULT '[]', checked REAL);
        -- контакты автора: только то, что он сам опубликовал; source — откуда взят
        CREATE TABLE IF NOT EXISTS contacts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, article_id INTEGER, channel TEXT, value TEXT, source TEXT,
            confidence TEXT, status TEXT DEFAULT 'unknown', note TEXT DEFAULT '', found REAL, checked REAL,
            UNIQUE(article_id, channel, value));
        -- сообщения: draft → approved → sent → read → replied; skipped / failed / stopped
        CREATE TABLE IF NOT EXISTS outreach(
            id INTEGER PRIMARY KEY AUTOINCREMENT, article_id INTEGER, contact_id INTEGER, channel TEXT, value TEXT,
            kind TEXT DEFAULT 'first', text TEXT, status TEXT DEFAULT 'draft', note TEXT DEFAULT '',
            peer_id INTEGER, msg_id INTEGER, created REAL, approved REAL, sent REAL, read REAL, replied REAL,
            card_msg TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS outreach_status ON outreach(status);
        CREATE TABLE IF NOT EXISTS stoplist(key TEXT PRIMARY KEY, reason TEXT, ts REAL);
        """)
        cols = {r[1] for r in _db.execute("PRAGMA table_info(articles)")}
        for col, ddl in (("match", "TEXT DEFAULT ''"), ("dsg_slug", "TEXT DEFAULT ''"), ("links", "TEXT DEFAULT '[]'"), ("excerpt", "TEXT DEFAULT ''")):
            if col not in cols:
                _db.execute(f"ALTER TABLE articles ADD COLUMN {col} {ddl}")
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
    for k in ("stats", "author_stats", "ai_json", "links"):
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
    for k in ("stats", "author_stats", "ai_json", "links"):
        if k not in d:
            continue
        try:
            d[k] = json.loads(d.get(k) or ("[]" if k == "links" else "{}"))
        except ValueError:
            d[k] = {}
    return d


# --- авторы дизайнерс
def dsg_authors() -> list[dict]:
    out = []
    for r in db().execute("SELECT * FROM dsg_authors"):
        d = dict(r)
        d["links"] = json.loads(d["links"] or "[]")
        out.append(d)
    return out


def save_dsg_author(slug: str, name: str, links: list[str]):
    db().execute("INSERT OR REPLACE INTO dsg_authors(slug,name,links,checked) VALUES(?,?,?,?)",
                 (slug, name, json.dumps(links, ensure_ascii=False), time.time()))
    db().commit()


def dsg_checked() -> dict[str, float]:
    return {r[0]: r[1] or 0 for r in db().execute("SELECT slug, checked FROM dsg_authors")}


# --- контакты
def add_contact(article_id: int, channel: str, value: str, source: str, confidence: str) -> int:
    cur = db().execute("INSERT OR IGNORE INTO contacts(article_id,channel,value,source,confidence,found) VALUES(?,?,?,?,?,?)",
                       (article_id, channel, value, source, confidence, time.time()))
    db().commit()
    if cur.rowcount:
        return cur.lastrowid
    r = db().execute("SELECT id FROM contacts WHERE article_id=? AND channel=? AND value=?", (article_id, channel, value)).fetchone()
    return r[0]


def contacts_for(article_id: int) -> list[dict]:
    return [dict(r) for r in db().execute("SELECT * FROM contacts WHERE article_id=? ORDER BY id", (article_id,))]


def contact(cid: int) -> dict | None:
    r = db().execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def set_contact(cid: int, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    db().execute(f"UPDATE contacts SET {sets} WHERE id=?", (*fields.values(), cid))
    db().commit()


def forget_contacts(older_than_days: int) -> int:
    """Контакты тех, кому так и не написали, — удаляем через N дней."""
    cut = time.time() - older_than_days * 86400
    n = db().execute("DELETE FROM contacts WHERE found<? AND id NOT IN (SELECT contact_id FROM outreach "
                     "WHERE contact_id IS NOT NULL AND status NOT IN ('draft','skipped'))", (cut,)).rowcount
    db().commit()
    return n


# --- сообщения
def add_outreach(article_id: int, contact_id: int | None, channel: str, value: str, text: str, kind: str = "first") -> int:
    cur = db().execute("INSERT INTO outreach(article_id,contact_id,channel,value,kind,text,created) VALUES(?,?,?,?,?,?,?)",
                       (article_id, contact_id, channel, value, kind, text, time.time()))
    db().commit()
    return cur.lastrowid


def outreach(oid: int) -> dict | None:
    r = db().execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone()
    return dict(r) if r else None


def set_outreach(oid: int, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    db().execute(f"UPDATE outreach SET {sets} WHERE id=?", (*fields.values(), oid))
    db().commit()


def outreach_by(status: str, limit: int = 100) -> list[dict]:
    return [dict(r) for r in db().execute("SELECT * FROM outreach WHERE status=? ORDER BY approved, id LIMIT ?", (status, limit))]


def outreach_by_peer(peer_id: int) -> dict | None:
    r = db().execute("SELECT * FROM outreach WHERE peer_id=? AND status IN ('sent','read','replied') "
                     "ORDER BY sent DESC LIMIT 1", (peer_id,)).fetchone()
    return dict(r) if r else None


def already_contacted(value: str, author_url: str = "") -> bool:
    """Одному человеку — одно первое сообщение: по контакту и по профилю автора."""
    v = value.lower()
    if db().execute("SELECT 1 FROM outreach WHERE lower(value)=? AND status IN ('draft','approved','sent','read','replied')",
                    (v,)).fetchone():
        return True
    if author_url and db().execute(
            "SELECT 1 FROM outreach o JOIN articles a ON a.id=o.article_id WHERE a.author_url=? "
            "AND o.status IN ('approved','sent','read','replied')", (author_url,)).fetchone():
        return True
    return False


def sent_since(ts: float) -> int:
    return db().execute("SELECT COUNT(*) FROM outreach WHERE sent>=? AND kind='first'", (ts,)).fetchone()[0]


def first_sent_ts() -> float:
    r = db().execute("SELECT MIN(sent) FROM outreach WHERE sent IS NOT NULL").fetchone()
    return r[0] or 0.0


def recent_texts(limit: int = 15) -> list[str]:
    return [r[0] for r in db().execute(
        "SELECT text FROM outreach WHERE status IN ('approved','sent','read','replied') AND kind='first' "
        "ORDER BY id DESC LIMIT ?", (limit,))]


def outreach_stats(since: float) -> dict:
    q = lambda col: db().execute(f"SELECT COUNT(*) FROM outreach WHERE {col}>=?", (since,)).fetchone()[0]  # noqa: E731
    return {"drafts": q("created"), "sent": q("sent"), "read": q("read"), "replied": q("replied")}


# --- стоп-лист
def stop(key: str, reason: str = ""):
    if key:
        db().execute("INSERT OR REPLACE INTO stoplist(key,reason,ts) VALUES(?,?,?)", (key.lower(), reason, time.time()))
        db().commit()


def stopped(*keys: str) -> bool:
    ks = [k.lower() for k in keys if k]
    if not ks:
        return False
    q = ",".join("?" * len(ks))
    return db().execute(f"SELECT 1 FROM stoplist WHERE key IN ({q})", ks).fetchone() is not None


def already_contacted_other(oid: int, value: str, author_url: str = "") -> bool:
    """Как already_contacted, но не считая само это сообщение (оно уже «approved»)."""
    if db().execute("SELECT 1 FROM outreach WHERE id!=? AND kind='first' AND lower(value)=? "
                    "AND status IN ('sent','read','replied')", (oid, value.lower())).fetchone():
        return True
    if author_url and db().execute(
            "SELECT 1 FROM outreach o JOIN articles a ON a.id=o.article_id WHERE o.id!=? AND o.kind='first' "
            "AND a.author_url=? AND o.status IN ('sent','read','replied')", (oid, author_url)).fetchone():
        return True
    return False


def has_followup(article_id: int) -> bool:
    return db().execute("SELECT 1 FROM outreach WHERE article_id=? AND kind='followup'", (article_id,)).fetchone() is not None


def outreach_for_article(article_id: int) -> list[dict]:
    return [dict(r) for r in db().execute("SELECT * FROM outreach WHERE article_id=? ORDER BY id", (article_id,))]
