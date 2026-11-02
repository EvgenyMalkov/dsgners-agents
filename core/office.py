"""Состояние офиса: статусы агентов, лента событий и история задач (SQLite) + рассылка подписчикам."""
import asyncio
import json
import sqlite3
import time
import uuid

from core import config

AGENTS = {
    "designer": {"title": "Графический дизайнер", "active": True},
    "webdesigner": {"title": "Веб-дизайнер", "active": True},
    "smm": {"title": "SMM-щик", "active": True},
    "seo": {"title": "SEO-шник", "active": True},
}


class Office:
    def __init__(self):
        self.db = sqlite3.connect(config.DATA / "office.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, agent TEXT, kind TEXT, text TEXT, job_id TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, agent TEXT, task TEXT, full_task TEXT, status TEXT,
            files TEXT DEFAULT '[]', caption TEXT DEFAULT '', cost REAL DEFAULT 0, source TEXT, parent TEXT,
            reason TEXT DEFAULT '', created REAL, finished REAL);
        """)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
        if "meta" not in cols:
            self.db.execute("ALTER TABLE jobs ADD COLUMN meta TEXT DEFAULT '{}'")
        self.db.commit()
        # после перезапуска незавершённые задачи считаем прерванными
        self.db.execute("UPDATE jobs SET status='error', caption='Прервано перезапуском сервера' WHERE status IN ('queued','running')")
        self.db.commit()
        self.status = {a: {"id": a, "status": "idle", "task": ""} for a in AGENTS}
        self.subs: set[asyncio.Queue] = set()

    # --- рассылка
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subs.add(q)
        return q

    def unsubscribe(self, q):
        self.subs.discard(q)

    def _broadcast(self, msg: dict):
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self.subs.discard(q)

    # --- статусы и события
    def set_status(self, agent: str, status: str, task: str | None = None):
        s = self.status[agent]
        s["status"] = status
        if task is not None:
            s["task"] = task
        if status == "idle":
            s["task"] = ""
        self._broadcast({"type": "status", "agent": agent, "status": status, "task": s["task"]})

    def emit(self, agent: str, kind: str, text: str, job_id: str | None = None):
        ts = time.time()
        self.db.execute("INSERT INTO events(ts,agent,kind,text,job_id) VALUES(?,?,?,?,?)", (ts, agent, kind, text, job_id))
        self.db.commit()
        self._broadcast({"type": "event", "ts": ts, "agent": agent, "kind": kind, "text": text, "job_id": job_id})

    def recent_events(self, limit=300) -> list[dict]:
        rows = self.db.execute("SELECT ts,agent,kind,text,job_id FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    # --- задачи
    def create_job(self, agent, task, full_task, source, parent=None) -> str:
        jid = uuid.uuid4().hex[:10]
        self.db.execute("INSERT INTO jobs(id,agent,task,full_task,status,source,parent,created) VALUES(?,?,?,?,?,?,?,?)",
                        (jid, agent, task, full_task, "queued", source, parent, time.time()))
        self.db.commit()
        self._job_changed(jid)
        return jid

    def update_job(self, jid, **fields):
        for k in ("files", "meta"):
            if k in fields:
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        sets = ",".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), jid))
        self.db.commit()
        self._job_changed(jid)

    def _job_changed(self, jid):
        job = self.get_job(jid)
        if job:
            self._broadcast({"type": "job", "job": job})

    def get_job(self, jid) -> dict | None:
        r = self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["files"] = json.loads(d["files"] or "[]")
        d["meta"] = json.loads(d.get("meta") or "{}")
        d.pop("full_task", None)
        return d

    def full_task(self, jid) -> str:
        r = self.db.execute("SELECT full_task FROM jobs WHERE id=?", (jid,)).fetchone()
        return r[0] if r else ""

    def list_jobs(self, agent=None, limit=60) -> list[dict]:
        q, args = "SELECT id FROM jobs", []
        if agent:
            q += " WHERE agent=?"
            args.append(agent)
        q += " ORDER BY created DESC LIMIT ?"
        args.append(limit)
        return [self.get_job(r[0]) for r in self.db.execute(q, args).fetchall()]

    def pending_by_agent(self) -> dict:
        rows = self.db.execute("SELECT agent, COUNT(*) FROM jobs WHERE status='review' GROUP BY agent").fetchall()
        return {r[0]: r[1] for r in rows}


office = Office()
