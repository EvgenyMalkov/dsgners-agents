"""Доступ агентов к Figma (только чтение) через REST API.

.env:
  FIGMA_TOKEN=figd_...                       — personal access token, право «File content: read»
  FIGMA_FILES=dev:KEY1,brand:KEY2            — какие файлы можно смотреть (метка:ключ из ссылки figma.com/design/KEY/…)

Инструменты:
  figma_find   — найти экраны/фреймы/компоненты по названию
  figma_export — выгрузить найденный узел в PNG (картинку можно передать дизайнеру или вставить в визуал)
"""
import io
import json
import re
import time

import aiohttp
from PIL import Image

from core import config
from core.agent import Ctx, image_block

API = "https://api.figma.com/v1"
BRAND_FILE = "mOEJGc0jVc8yhLsuvEVi8M"   # «Коммуникационка дизайнерс» — гайдбук
_cache: dict = {}                        # key -> (ts, nodes)
CACHE_TTL = 600
MAX_SIDE = 3000


def files() -> dict[str, str]:
    raw = config.env("FIGMA_FILES", "")
    res = {}
    for part in raw.split(","):
        if ":" in part:
            label, key = part.split(":", 1)
            res[label.strip()] = key.strip()
        elif part.strip():
            res[part.strip()] = part.strip()
    res.setdefault("brand", BRAND_FILE)
    return res


def _key(file: str) -> str:
    fs = files()
    if file in fs:
        return fs[file]
    m = re.search(r"figma\.com/(?:design|file)/([A-Za-z0-9]{10,})", file or "")
    if m and m.group(1) in fs.values():
        return m.group(1)
    raise ValueError(f"Нет доступа к файлу «{file}». Доступны: {', '.join(fs)}")


async def _get(path: str, **params) -> dict:
    token = config.env("FIGMA_TOKEN", "")
    if not token:
        raise RuntimeError("Figma не подключена: нет FIGMA_TOKEN в .env на сервере.")
    async with aiohttp.ClientSession(headers={"X-Figma-Token": token}, timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.get(API + path, params=params) as r:
            if r.status == 403:
                raise RuntimeError("Figma: нет доступа (токен не видит этот файл или истёк).")
            if r.status == 429:
                raise RuntimeError("Figma: слишком много запросов, попробуй через минуту.")
            if r.status >= 400:
                raise RuntimeError(f"Figma: ошибка {r.status}: {(await r.text())[:200]}")
            return await r.json()


KINDS = {"FRAME", "SECTION", "COMPONENT", "COMPONENT_SET", "GROUP", "INSTANCE"}


def _cached(key):
    hit = _cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < CACHE_TTL else None


async def _pages(key: str) -> list[dict]:
    """Страницы файла (лёгкий запрос depth=1). Разделители вида «----» пропускаем."""
    if (c := _cached(("pages", key))) is not None:
        return c
    data = await _get(f"/files/{key}", depth=1)
    pages = [{"id": p["id"], "name": p.get("name", "")} for p in data.get("document", {}).get("children", [])
             if re.search(r"[\wА-Яа-яЁё]", p.get("name", ""))]
    _cache[("pages", key)] = (time.time(), pages)
    return pages


async def _page_nodes(key: str, pages: list[dict]) -> list[dict]:
    """Фреймы внутри выбранных страниц (до 2 уровней вложенности), с кешем по странице."""
    out, need = [], []
    for p in pages:
        c = _cached(("page", key, p["id"]))
        if c is None:
            need.append(p)
        else:
            out += c
    for i in range(0, len(need), 8):
        chunk = need[i:i + 8]
        data = await _get(f"/files/{key}/nodes", ids=",".join(p["id"] for p in chunk), depth=3)
        for p in chunk:
            doc = ((data.get("nodes") or {}).get(p["id"]) or {}).get("document") or {}
            items = []

            def walk(node, path, depth):
                for ch in node.get("children", []) or []:
                    if ch.get("type") in KINDS:
                        bb = ch.get("absoluteBoundingBox") or {}
                        items.append({"id": ch["id"], "name": ch.get("name", ""), "type": ch["type"],
                                      "page": p["name"], "path": " / ".join(path + [ch.get("name", "")]),
                                      "w": round(bb.get("width", 0)), "h": round(bb.get("height", 0))})
                    if depth < 2:
                        walk(ch, path + [ch.get("name", "")], depth + 1)

            walk(doc, [p["name"]], 1)
            _cache[("page", key, p["id"])] = (time.time(), items)
            out += items
    return out


FIND = {
    "name": "figma_find",
    "description": "Найти в Figma экраны, фреймы, секции и компоненты по названию (например «коллабы», «онбординг», "
                   "«главная мобилка»). Возвращает id узлов для figma_export. Файлы: метки из списка доступных.",
    "input_schema": {"type": "object", "properties": {
        "query": {"type": "string", "description": "слова из названия; пусто — показать верхние разделы файла"},
        "file": {"type": "string", "description": "метка файла (dev, brand…) — по умолчанию dev, если он есть"},
    }, "required": ["query"]},
}

EXPORT = {
    "name": "figma_export",
    "description": "Выгрузить узел Figma в PNG и посмотреть его. Файл сохранится в твоей задаче — его путь можно "
                   "передать в шаблон визуала (поле image) или коллеге.",
    "input_schema": {"type": "object", "properties": {
        "node_id": {"type": "string", "description": "id из figma_find, напр. 12:345"},
        "file": {"type": "string"},
        "name": {"type": "string", "description": "имя файла латиницей"},
        "scale": {"type": "integer", "enum": [2, 3, 4], "description": "чёткость; мобильный экран (375) — 3, "
                  "если автопроверка пишет «картинка мелкая» — 4. По умолчанию 2"},
    }, "required": ["node_id", "name"]},
}


def _default_file() -> str:
    fs = files()
    return "dev" if "dev" in fs else next(iter(fs))


def _score(text: str, words: list[str]) -> int:
    t = text.lower()
    return sum(1 for w in words if w in t)


async def find(inp: dict, ctx: Ctx):
    file = inp.get("file") or _default_file()
    key = _key(file)
    if ctx.progress:
        await ctx.progress(f"ищу в Figma ({file}): {inp.get('query', '')[:40]}")
    pages = await _pages(key)
    words = [w[:6] for w in re.split(r"[\s,.]+", (inp.get("query") or "").lower()) if len(w) > 1]
    if not words:
        lines = [f"- страница {p['id']} | {p['name']}" for p in pages]
        return [{"type": "text", "text": f"Файл «{file}», страницы:\n" + "\n".join(lines) +
                 "\nИщи по названию фичи — найду фреймы внутри страницы."}]
    # 1) страницы, подходящие по названию; 2) если таких нет — первые 16 страниц файла
    ranked = sorted(pages, key=lambda p: -_score(p["name"], words))
    target = [p for p in ranked if _score(p["name"], words)][:3] or pages[:16]
    nodes = await _page_nodes(key, target)
    scored = []
    for n in nodes:
        sc = _score(n["path"], words) + 2 * _score(n["name"], words)
        top = n["path"].count(" / ") == 1  # фреймы верхнего уровня страницы — главные экраны
        scored.append((sc + (1 if top else 0), -len(n["path"]), n))
    hits = [n for sc, _, n in sorted(scored, key=lambda x: (-x[0], x[1])) if sc > 0][:30]
    if not hits:
        ctx.meta["figma_empty"] = ctx.meta.get("figma_empty", 0) + 1   # 2 пустых поиска — экранов правда нет
        return [{"type": "text", "text": f"В файле «{file}» по «{inp.get('query')}» ничего. "
                                         f"Вызови figma_find с пустым query — покажу список страниц."}]
    lines = [f"- {n['id']} | {n['type']} | {n['w']}×{n['h']} | {n['path']}" for n in hits]
    return [{"type": "text", "text": f"Файл «{file}». Найдено (id | тип | размер | страница / путь):\n" + "\n".join(lines)}]


async def export(inp: dict, ctx: Ctx):
    file = inp.get("file") or _default_file()
    key = _key(file)
    nid = inp["node_id"].replace("-", ":")
    if ctx.progress:
        await ctx.progress(f"выгружаю из Figma {nid}")
    scale = inp.get("scale") if inp.get("scale") in (2, 3, 4) else 2
    data = await _get(f"/images/{key}", ids=nid, format="png", scale=scale)
    url = (data.get("images") or {}).get(nid)
    if not url:
        return [{"type": "text", "text": f"Figma не отдала картинку для {nid} (узел пустой или скрыт)."}]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        async with s.get(url) as r:
            raw = await r.read()
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    # Длинный экран не ужимаем целиком (иначе 375×6000 превращался в мыло 150 px шириной):
    # ширину ограничиваем, а по высоте берём верх — витрины всё равно показывают верх экрана.
    if im.width > MAX_SIDE:
        im = im.resize((MAX_SIDE, round(im.height * MAX_SIDE / im.width)), Image.LANCZOS)
    if im.height > im.width * 2.4:
        im = im.crop((0, 0, im.width, round(im.width * 2.4)))
    from core.common_tools import safe_name
    p = ctx.job_dir / f"figma-{safe_name(inp['name'], 'frame')}.png"
    bg = Image.new("RGB", im.size, "white")
    bg.paste(im, mask=im.split()[3])
    bg.save(p, "PNG", optimize=True)
    return [{"type": "text", "text": f"Сохранено: {p} ({im.width}×{im.height}). Этот путь можно указать в image."},
            image_block(p, 1000)]


HANDLERS = {"figma_find": find, "figma_export": export}


def tools() -> list:
    """Инструменты Figma — только если токен задан (иначе агент о них не знает и не тратит токены)."""
    return [FIND, EXPORT] if config.env("FIGMA_TOKEN", "") else []


def system_note() -> str:
    if not config.env("FIGMA_TOKEN", ""):
        return ""
    return ("# Figma\nУ тебя есть доступ на чтение к файлам Figma: " + ", ".join(files()) +
            ". «dev» — макеты сайта DSGNERS 3.0: каждая фича — отдельная страница (напр. «Совместные публикации "
            "(коллабы)», «AI-поиск», «Бро-подписка», «Онбординг новых пользователей»), разделы сайта — страницы с эмодзи "
            "(«🏚 Лента», «✏️ Редактор», «🚹 Профиль»…). «brand» — гайдбук. Нужен экран фичи — найди (figma_find по "
            "названию фичи) и выгрузи (figma_export) самый подходящий фрейм верхнего уровня; не выдумывай интерфейс.")
