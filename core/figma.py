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
MAX_SIDE = 2400


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


async def _nodes(key: str) -> list[dict]:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    data = await _get(f"/files/{key}", depth=4)
    out = []

    def walk(node, page, path, depth):
        for ch in node.get("children", []) or []:
            if ch.get("type") in KINDS:
                bb = ch.get("absoluteBoundingBox") or {}
                out.append({"id": ch["id"], "name": ch.get("name", ""), "type": ch["type"], "page": page,
                            "path": " / ".join(path + [ch.get("name", "")]),
                            "w": round(bb.get("width", 0)), "h": round(bb.get("height", 0))})
            if depth < 3:
                walk(ch, page, path + [ch.get("name", "")], depth + 1)

    for pg in data.get("document", {}).get("children", []):
        walk(pg, pg.get("name", ""), [pg.get("name", "")], 1)
    _cache[key] = (time.time(), out)
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
    }, "required": ["node_id", "name"]},
}


def _default_file() -> str:
    fs = files()
    return "dev" if "dev" in fs else next(iter(fs))


async def find(inp: dict, ctx: Ctx):
    file = inp.get("file") or _default_file()
    key = _key(file)
    if ctx.progress:
        await ctx.progress(f"ищу в Figma ({file}): {inp.get('query', '')[:40]}")
    nodes = await _nodes(key)
    words = [w for w in re.split(r"\s+", (inp.get("query") or "").lower()) if w]
    if words:
        scored = []
        for n in nodes:
            hay = n["path"].lower()
            score = sum(1 for w in words if w in hay) + sum(2 for w in words if w in n["name"].lower())
            if score:
                scored.append((score, -len(n["path"]), n))
        hits = [n for _, _, n in sorted(scored, key=lambda x: (-x[0], x[1]))][:25]
    else:
        hits = [n for n in nodes if n["path"].count(" / ") <= 1][:40]
    if not hits:
        return [{"type": "text", "text": f"В файле «{file}» ничего не нашлось по «{inp.get('query')}». "
                                         f"Попробуй другие слова или пустой запрос — покажу разделы."}]
    lines = [f"- {n['id']} | {n['type']} | {n['w']}×{n['h']} | {n['path']}" for n in hits]
    return [{"type": "text", "text": f"Файл «{file}». Найдено:\n" + "\n".join(lines)}]


async def export(inp: dict, ctx: Ctx):
    file = inp.get("file") or _default_file()
    key = _key(file)
    nid = inp["node_id"].replace("-", ":")
    if ctx.progress:
        await ctx.progress(f"выгружаю из Figma {nid}")
    data = await _get(f"/images/{key}", ids=nid, format="png", scale=2)
    url = (data.get("images") or {}).get(nid)
    if not url:
        return [{"type": "text", "text": f"Figma не отдала картинку для {nid} (узел пустой или скрыт)."}]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        async with s.get(url) as r:
            raw = await r.read()
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    im.thumbnail((MAX_SIDE, MAX_SIDE))
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
            ". «dev» — макеты новых фич сайта, «brand» — гайдбук. Нужен экран фичи или элемент интерфейса — "
            "найди его (figma_find) и выгрузи (figma_export), не выдумывай интерфейс.")
