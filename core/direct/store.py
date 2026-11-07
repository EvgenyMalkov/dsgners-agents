"""SQLite data/direct.db: что Директ-агент знает между запусками."""
import json
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit

from core import config

_db: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _db
    if _db is None:
        _db = sqlite3.connect(config.DATA / "direct.db", check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.executescript("""
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS runs(name TEXT PRIMARY KEY, ts REAL, ok INTEGER, note TEXT);
        CREATE TABLE IF NOT EXISTS campaigns(
            id INTEGER PRIMARY KEY, name TEXT, type TEXT, state TEXT, status TEXT, note TEXT DEFAULT '',
            daily_budget REAL, seen REAL);
        CREATE TABLE IF NOT EXISTS ads(
            id INTEGER PRIMARY KEY, campaign_id INTEGER, group_id INTEGER, state TEXT, status TEXT,
            note TEXT DEFAULT '', type TEXT, href TEXT DEFAULT '', article TEXT DEFAULT '', title TEXT DEFAULT '', seen REAL);
        -- день × кампания × объявление × сеть × устройство: деньги из Директа, визиты/отказы — из Метрики через Директ
        CREATE TABLE IF NOT EXISTS stats_ads(
            day TEXT, campaign_id INTEGER, ad_id INTEGER, network TEXT, device TEXT,
            impressions INTEGER, clicks INTEGER, cost REAL, sessions INTEGER, bounces INTEGER,
            PRIMARY KEY(day, campaign_id, ad_id, network, device));
        -- день × кампания × площадка
        CREATE TABLE IF NOT EXISTS stats_places(
            day TEXT, campaign_id INTEGER, placement TEXT, network TEXT,
            impressions INTEGER, clicks INTEGER, cost REAL, sessions INTEGER, bounces INTEGER,
            PRIMARY KEY(day, campaign_id, placement, network));
        -- площадки: proposed (предложена к исключению) | excluded | keep (не трогать)
        CREATE TABLE IF NOT EXISTS places(placement TEXT PRIMARY KEY, status TEXT, note TEXT DEFAULT '', ts REAL);
        -- журнал всех изменений агента в Директе — что, когда, почему (для отката)
        CREATE TABLE IF NOT EXISTS actions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, campaign_id INTEGER, payload TEXT, reason TEXT,
            ok INTEGER, note TEXT DEFAULT '');
        """)
        _db.commit()
    return _db


# --- ключ-значение и запуски
def get(key: str, default=None):
    r = db().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def put(key: str, value):
    db().execute("INSERT OR REPLACE INTO kv(key,value,ts) VALUES(?,?,?)",
                 (key, json.dumps(value, ensure_ascii=False), time.time()))
    db().commit()


def last_run(name: str) -> float:
    r = db().execute("SELECT ts FROM runs WHERE name=? AND ok=1", (name,)).fetchone()
    return r[0] if r else 0.0


def mark_run(name: str, ok: bool, note: str = ""):
    if ok:
        db().execute("INSERT OR REPLACE INTO runs(name,ts,ok,note) VALUES(?,?,?,?)", (name, time.time(), 1, note))
    else:   # неудача не сдвигает время последнего успешного запуска
        db().execute("INSERT INTO runs(name,ts,ok,note) VALUES(?,?,0,?) "
                     "ON CONFLICT(name) DO UPDATE SET ok=0, note=excluded.note", (name, time.time(), note))
    db().commit()


def runs() -> list[dict]:
    return [dict(r) for r in db().execute("SELECT * FROM runs ORDER BY name")]


# --- статьи
def article_of(href: str) -> str:
    """Адрес статьи из ссылки объявления: без UTM, якоря и хвостового слэша."""
    if not href:
        return ""
    p = urlsplit(href.strip())
    host = p.netloc.lower().removeprefix("www.")
    return urlunsplit(("https", host, p.path.rstrip("/") or "/", "", ""))


# --- кампании и объявления
def save_campaigns(rows: list[dict]) -> list[tuple[dict, str]]:
    """Сохранить кампании. Вернёт [(кампания, что изменилось)] — для алертов."""
    changes, now = [], time.time()
    for c in rows:
        old = db().execute("SELECT * FROM campaigns WHERE id=?", (c["Id"],)).fetchone()
        budget = ((c.get("DailyBudget") or {}).get("Amount") or 0) / 1_000_000
        new = {"id": c["Id"], "name": c.get("Name", ""), "type": c.get("Type", ""), "state": c.get("State", ""),
               "status": c.get("Status", ""), "note": c.get("StatusClarification") or "", "daily_budget": budget}
        if old and old["state"] != new["state"]:
            changes.append((new, f"состояние {old['state']} → {new['state']}"))
        if old and old["status"] != new["status"] and new["status"] == "REJECTED":
            changes.append((new, "кампания отклонена модерацией"))
        db().execute("INSERT OR REPLACE INTO campaigns(id,name,type,state,status,note,daily_budget,seen) "
                     "VALUES(:id,:name,:type,:state,:status,:note,:daily_budget,:seen)", dict(new, seen=now))
    db().commit()
    return changes


def campaigns() -> list[dict]:
    return [dict(r) for r in db().execute("SELECT * FROM campaigns ORDER BY id")]


def campaign_name(cid: int) -> str:
    r = db().execute("SELECT name FROM campaigns WHERE id=?", (cid,)).fetchone()
    return r[0] if r else str(cid)


def save_ads(rows: list[dict], href_of, title_of) -> list[dict]:
    """Сохранить объявления. Вернёт объявления, которые только что стали отклонёнными (REJECTED)."""
    rejected, now = [], time.time()
    for a in rows:
        old = db().execute("SELECT status FROM ads WHERE id=?", (a["Id"],)).fetchone()
        href = href_of(a)
        new = {"id": a["Id"], "campaign_id": a.get("CampaignId"), "group_id": a.get("AdGroupId"),
               "state": a.get("State", ""), "status": a.get("Status", ""), "note": a.get("StatusClarification") or "",
               "type": a.get("Type", ""), "href": href, "article": article_of(href), "title": title_of(a), "seen": now}
        if new["status"] == "REJECTED" and old and old[0] != "REJECTED":
            rejected.append(new)
        db().execute("INSERT OR REPLACE INTO ads(id,campaign_id,group_id,state,status,note,type,href,article,title,seen) "
                     "VALUES(:id,:campaign_id,:group_id,:state,:status,:note,:type,:href,:article,:title,:seen)", new)
    db().commit()
    return rejected


# --- статистика
def _i(v) -> int:
    return int(v or 0)


def _f(v) -> float:
    return float(v or 0)


def save_ad_stats(rows: list[dict], days: list[str]):
    """Перезаписать статистику объявлений за дни (данные Метрики за прошлые дни досчитываются)."""
    d = db()
    d.executemany("DELETE FROM stats_ads WHERE day=?", [(x,) for x in days])
    for r in rows:
        d.execute("INSERT OR REPLACE INTO stats_ads VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (r["Date"], _i(r.get("CampaignId")), _i(r.get("AdId")), r.get("AdNetworkType") or "",
                   r.get("Device") or "", _i(r.get("Impressions")), _i(r.get("Clicks")), _f(r.get("Cost")),
                   _i(r.get("Sessions")), _i(r.get("Bounces"))))
    d.commit()


def save_place_stats(rows: list[dict], days: list[str]):
    d = db()
    d.executemany("DELETE FROM stats_places WHERE day=?", [(x,) for x in days])
    for r in rows:
        d.execute("INSERT OR REPLACE INTO stats_places VALUES(?,?,?,?,?,?,?,?,?)",
                  (r["Date"], _i(r.get("CampaignId")), (r.get("Placement") or "—").strip(), r.get("AdNetworkType") or "",
                   _i(r.get("Impressions")), _i(r.get("Clicks")), _f(r.get("Cost")),
                   _i(r.get("Sessions")), _i(r.get("Bounces"))))
    d.commit()


def has_stats() -> bool:
    return bool(db().execute("SELECT 1 FROM stats_ads LIMIT 1").fetchone())


SUMS = ("SUM(impressions) impressions, SUM(clicks) clicks, SUM(cost) cost, "
        "SUM(sessions) sessions, SUM(bounces) bounces")


def totals(d1: str, d2: str, by: str = "") -> list[dict]:
    """Суммы за период [d1; d2] по объявлениям. by — колонка группировки (network, device, campaign_id) или ''."""
    grp = f"{by}, " if by else ""
    q = f"SELECT {grp}{SUMS} FROM stats_ads WHERE day BETWEEN ? AND ?" + (f" GROUP BY {by}" if by else "")
    return [dict(r) for r in db().execute(q, (d1, d2))]


def by_article(d1: str, d2: str) -> list[dict]:
    q = (f"SELECT COALESCE(NULLIF(a.article,''), 'объявление ' || s.ad_id) article, "
         f"MAX(a.title) ad_title, COUNT(DISTINCT s.ad_id) ads, {SUMS.replace('SUM(', 'SUM(s.')} "
         "FROM stats_ads s LEFT JOIN ads a ON a.id = s.ad_id WHERE s.day BETWEEN ? AND ? GROUP BY 1")
    return [dict(r) for r in db().execute(q, (d1, d2))]


def by_placement(d1: str, d2: str, network: str = "AD_NETWORK") -> list[dict]:
    q = (f"SELECT placement, GROUP_CONCAT(DISTINCT campaign_id) campaigns, {SUMS} FROM stats_places "
         "WHERE day BETWEEN ? AND ? AND network=? GROUP BY placement")
    return [dict(r) for r in db().execute(q, (d1, d2, network))]


def campaigns_with_place(placement: str, since: str) -> list[int]:
    return [r[0] for r in db().execute("SELECT DISTINCT campaign_id FROM stats_places WHERE placement=? AND day>=?",
                                       (placement, since))]


# --- площадки
def place_status(placement: str) -> str:
    r = db().execute("SELECT status FROM places WHERE placement=?", (placement.lower(),)).fetchone()
    return r[0] if r else ""


def set_place(placement: str, status: str, note: str = ""):
    db().execute("INSERT OR REPLACE INTO places(placement,status,note,ts) VALUES(?,?,?,?)",
                 (placement.lower(), status, note, time.time()))
    db().commit()


def places(status: str) -> list[dict]:
    return [dict(r) for r in db().execute("SELECT * FROM places WHERE status=? ORDER BY ts DESC", (status,))]


# --- журнал
def log_action(kind: str, campaign_id: int | None, payload, reason: str, ok: bool, note: str = ""):
    db().execute("INSERT INTO actions(ts,kind,campaign_id,payload,reason,ok,note) VALUES(?,?,?,?,?,?,?)",
                 (time.time(), kind, campaign_id, json.dumps(payload, ensure_ascii=False), reason, int(ok), note))
    db().commit()


def actions(limit: int = 20) -> list[dict]:
    return [dict(r) for r in db().execute("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,))]
