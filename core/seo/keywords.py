"""Семантика: выгрузки Вордстата (CSV/XLSX, присланные в бот) + запросы из Вебмастера.

Вордстат-выгрузку владелец кидает боту файлом с подписью «вордстат». Парсер понимает обычные выгрузки:
первая текстовая колонка — фраза, первая числовая — частотность. Повторная выгрузка обновляет частоты.
"""
import csv
import io
import re
import time

from core.seo import store


def _db():
    d = store.db()
    d.execute("""CREATE TABLE IF NOT EXISTS keywords(phrase TEXT PRIMARY KEY, freq INTEGER, source TEXT, added REAL)""")
    return d


def _num(v) -> int | None:
    s = re.sub(r"[\s ]", "", str(v or ""))
    return int(float(s.replace(",", "."))) if re.fullmatch(r"\d+([.,]\d+)?", s) else None


def _rows_from_bytes(name: str, data: bytes) -> list[list]:
    if name.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = []
        for ws in wb.worksheets:
            rows += [list(r) for r in ws.iter_rows(values_only=True)]
        return rows
    text = data.decode("utf-8-sig", errors="ignore")
    if text.count("�") > 20 or not re.search(r"[а-яА-Я]", text):
        text = data.decode("cp1251", errors="ignore")
    dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t") if text.strip() else csv.excel
    return [r for r in csv.reader(io.StringIO(text), dialect)]


def import_file(name: str, data: bytes, source: str = "wordstat") -> int:
    """Загрузить выгрузку. Вернёт число фраз."""
    n = 0
    d = _db()
    for r in _rows_from_bytes(name, data):
        cells = [c for c in r if c not in (None, "")]
        phrase = next((str(c).strip() for c in cells if _num(c) is None and re.search(r"[a-zа-яё]", str(c).lower())), "")
        freq = next((_num(c) for c in cells if _num(c) is not None), None)
        if not phrase or freq is None or len(phrase) > 200 or phrase.lower() in ("фраза", "запрос", "phrase"):
            continue
        phrase = re.sub(r"\s+", " ", phrase.lower().replace("+", "").replace("\"", "").replace("!", "")).strip()
        d.execute("INSERT OR REPLACE INTO keywords(phrase,freq,source,added) VALUES(?,?,?,?)",
                  (phrase, freq, source, time.time()))
        n += 1
    d.commit()
    return n


def count() -> int:
    return _db().execute("SELECT COUNT(*) FROM keywords").fetchone()[0]


def stems(s: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-zа-яё0-9]+", s.lower()) if len(w) > 2}


def search(topic: str, limit=40) -> list[dict]:
    """Фразы Вордстата по теме, по убыванию частоты."""
    t = stems(topic)
    if not t:
        return []
    out = []
    for phrase, freq in _db().execute("SELECT phrase, freq FROM keywords ORDER BY freq DESC LIMIT 50000"):
        hit = len(t & stems(phrase))
        if hit >= min(2, len(t)):
            out.append({"phrase": phrase, "freq": freq, "hit": hit})
    out.sort(key=lambda x: (-x["hit"], -x["freq"]))
    return out[:limit]


def top(limit=300) -> list[dict]:
    return [{"phrase": p, "freq": f} for p, f in _db().execute(
        "SELECT phrase, freq FROM keywords ORDER BY freq DESC LIMIT ?", (limit,))]


def covered(phrase: str, titles: list[str]) -> bool:
    """Есть ли у нас страница, заголовок которой покрывает фразу (грубо: ≥70% основ слов фразы)."""
    ps = stems(phrase)
    if not ps:
        return True
    return any(len(ps & stems(t)) >= max(1, round(len(ps) * 0.7)) for t in titles)
