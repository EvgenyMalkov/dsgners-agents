"""Общие инструменты, которые агенты подключают себе в tools.py.

read_page      — прочитать публичную страницу (текст + SEO-факты)
save_document  — сохранить текстовый результат (отчёт, ТЗ, план, пост) как файл .md
ask_colleague  — поручить задачу другому агенту и получить его результат (например, SMM → дизайнеру)
finish         — сдать работу на согласование
"""
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import aiohttp

from core.agent import Ctx

UA = "Mozilla/5.0 (compatible; dsgners-agents/1.0; +https://dsgners.ru)"
PAGE_LIMIT = 14_000


def safe_name(name: str, default="file") -> str:
    tr = str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", "abvgdeezziiklmnoprstufhccss_y_eua")
    s = re.sub(r"[^a-z0-9_-]+", "-", name.lower().translate(tr)).strip("-")
    return s[:50] or default


# ---------- read_page ----------
READ_PAGE = {
    "name": "read_page",
    "description": "Скачать публичную веб-страницу: заголовок, мета-теги, заголовки H1–H3, текст, ссылки, картинки без alt. "
                   "Для SEO-аудита и чтобы понять содержание статьи/страницы dsgners.ru.",
    "input_schema": {"type": "object", "properties": {
        "url": {"type": "string"},
        "with_text": {"type": "boolean", "description": "нужен ли основной текст страницы (по умолчанию да)"},
    }, "required": ["url"]},
}


def _strip(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _attr(tag: str, name: str) -> str:
    m = re.search(rf'(?i)\b{name}\s*=\s*("([^"]*)"|\'([^\']*)\')', tag)
    return unescape((m.group(2) or m.group(3) or "").strip()) if m else ""


async def fetch(url: str, limit_bytes=3_000_000) -> tuple[int, str, str]:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    async with aiohttp.ClientSession(headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=25)) as s:
        async with s.get(url, allow_redirects=True) as r:
            from core.seo.page import read_body
            raw, _ = await read_body(r, limit_bytes)
            return r.status, str(r.url), raw.decode(r.charset or "utf-8", errors="ignore")


async def read_page(inp: dict, ctx: Ctx):
    url = inp["url"]
    if ctx.progress:
        await ctx.progress(f"читаю {url[:60]}")
    status, final, html = await fetch(url)
    host = urlparse(final).netloc
    title = _strip(m.group(1)) if (m := re.search(r"(?is)<title[^>]*>(.*?)</title>", html)) else ""
    metas = {}
    for tag in re.findall(r"(?is)<meta\b[^>]*>", html):
        key = (_attr(tag, "name") or _attr(tag, "property")).lower()
        if key in ("description", "robots", "og:title", "og:description", "og:image", "keywords"):
            metas[key] = _attr(tag, "content")
    canon = next((_attr(t, "href") for t in re.findall(r"(?is)<link\b[^>]*>", html) if _attr(t, "rel").lower() == "canonical"), "")
    heads = [(lvl, _strip(t)) for lvl, t in re.findall(r"(?is)<h([1-3])[^>]*>(.*?)</h\1>", html)]
    links = [urljoin(final, _attr(a, "href")) for a in re.findall(r"(?is)<a\b[^>]*>", html) if _attr(a, "href")]
    internal = [l for l in links if urlparse(l).netloc == host]
    imgs = re.findall(r"(?is)<img\b[^>]*>", html)
    no_alt = [urljoin(final, _attr(i, "src")) for i in imgs if not re.search(r"(?i)[\s<]alt\s*=", i)]  # alt="" — декор, не ошибка
    text = _strip(html)

    out = [f"URL: {final} (HTTP {status})", f"title ({len(title)} симв.): {title}"]
    for k, v in metas.items():
        out.append(f"{k} ({len(v)} симв.): {v}")
    out.append(f"canonical: {canon or 'нет'}")
    out.append("Заголовки:\n" + ("\n".join(f"  H{l}: {t[:120]}" for l, t in heads[:40]) or "  нет"))
    out.append(f"Слов в тексте: ~{len(text.split())}. Ссылок: {len(links)} (внутренних {len(internal)}). "
               f"Картинок: {len(imgs)}, без alt: {len(no_alt)}")
    if no_alt[:5]:
        out.append("Без alt (первые): " + ", ".join(no_alt[:5]))
    if inp.get("with_text", True):
        out.append("Текст:\n" + text[:PAGE_LIMIT] + ("\n[…обрезано]" if len(text) > PAGE_LIMIT else ""))
    return [{"type": "text", "text": "\n".join(out)}]


# ---------- save_document ----------
SAVE_DOCUMENT = {
    "name": "save_document",
    "description": "Сохранить текстовый результат (отчёт, ТЗ, план, контент-план, текст статьи) как документ Markdown. "
                   "Повторный вызов с тем же name перезаписывает документ.",
    "input_schema": {"type": "object", "properties": {
        "name": {"type": "string", "description": "короткое имя файла латиницей"},
        "content": {"type": "string", "description": "Markdown"},
    }, "required": ["name", "content"]},
}


async def save_document(inp: dict, ctx: Ctx):
    p = ctx.job_dir / f"{safe_name(inp['name'], 'doc')}.md"
    p.write_text(inp["content"], "utf-8")
    if ctx.progress:
        await ctx.progress(f"сохранил документ {p.name}")
    return [{"type": "text", "text": f"Сохранено: {p.name} ({len(inp['content'])} симв.)"}]


# ---------- ask_colleague ----------
def ask_colleague_tool(allowed: dict[str, str]) -> dict:
    return {
        "name": "ask_colleague",
        "description": "Поручить задачу коллеге и дождаться результата (1–3 минуты). Готовые файлы коллеги "
                       "приложатся к твоей работе. Коллеги: " + "; ".join(f"{k} — {v}" for k, v in allowed.items()),
        "input_schema": {"type": "object", "properties": {
            "colleague": {"type": "string", "enum": list(allowed)},
            "task": {"type": "string", "description": "полная понятная задача: что, для чего, формат, тексты"},
        }, "required": ["colleague", "task"]},
    }


async def ask_colleague(inp: dict, ctx: Ctx):
    from core import config, runner  # поздний импорт: runner импортирует agent
    who = inp["colleague"]
    if who == ctx.agent:
        return [{"type": "text", "text": "Нельзя поручить задачу самому себе."}]
    if ctx.progress:
        await ctx.progress(f"попросил коллегу ({who})")
    task = inp["task"]
    # экраны, которые запросивший уже выгрузил из Figma, передаём коллеге автоматически — чтобы не делал «пустую» картинку
    shots = sorted(str(p) for p in ctx.job_dir.glob("figma-*.png"))
    if shots:
        task += ("\n\nЭкраны из Figma, которые я уже выгрузил (бери их в визуал — поле image/images, "
                 "шаблоны showcase / promo / promo-duo):\n" + "\n".join(f"- {s}" for s in shots[:8]))
    job = await runner.run_internal(who, task, parent=ctx.job_id, requester=ctx.agent)
    files = [config.OUTBOX / f for f in job.get("files", [])]
    ctx.meta.setdefault("from_colleagues", []).extend(job.get("files", []))
    names = [f"{f.parent.name}/{f.name}" for f in files]
    return [{"type": "text", "text": f"Коллега {who} ответил: {job.get('caption') or job.get('status')}\n"
                                     f"Файлы (используй эти имена): {', '.join(names) or 'нет'}"}]


# ---------- finish ----------
FINISH = {
    "name": "finish",
    "description": "Сдать работу владельцу на согласование. Все сохранённые документы и файлы от коллег "
                   "приложатся автоматически.",
    "input_schema": {"type": "object", "properties": {
        "summary": {"type": "string", "description": "что сделано и главные выводы/решения, 2–6 предложений"},
    }, "required": ["summary"]},
}


async def finish(inp: dict, ctx: Ctx):
    from core import config
    # рабочие материалы (выгрузки из Figma: экраны, ТЗ) — не результат, на согласование их не шлём
    own = sorted(p for p in ctx.job_dir.iterdir() if p.suffix in (".md", ".png", ".html") and p.name != "task.md"
                 and not p.name.startswith("figma-"))
    extra = [config.OUTBOX / f for f in ctx.meta.get("from_colleagues", [])]
    ctx.files = own + [p for p in extra if p.exists()]
    ctx.caption = inp.get("summary", "")
    if not ctx.files:
        # текстовый результат без файлов — сохраняем его как документ, чтобы было что согласовать
        p = ctx.job_dir / "result.md"
        p.write_text(ctx.caption, "utf-8")
        ctx.files = [p]
    ctx.done = True
    return [{"type": "text", "text": "Сдано на согласование."}]


COMMON_HANDLERS = {"read_page": read_page, "save_document": save_document,
                   "ask_colleague": ask_colleague, "finish": finish}
