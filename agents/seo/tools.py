"""Инструменты SEO-шника: аудит страниц и сайта, отчёты, ТЗ на правки."""
import re
from urllib.parse import urlparse

from core import common_tools as ct
from core.agent import Ctx

TOOLS = [
    {
        "name": "check_site",
        "description": "Техническая проверка сайта: доступность, редирект на https, robots.txt, sitemap.xml "
                       "(сколько URL, примеры), время ответа главной.",
        "input_schema": {"type": "object", "properties": {"site": {"type": "string", "description": "домен, напр. dsgners.ru"}},
                         "required": ["site"]},
    },
    ct.READ_PAGE,
    ct.SAVE_DOCUMENT,
    ct.ask_colleague_tool({
        "webdesigner": "веб-дизайнер: макет блока/страницы и ТЗ для разработки",
        "designer": "графический дизайнер: обложки и картинки для статей",
    }),
    ct.FINISH,
]


async def check_site(inp: dict, ctx: Ctx):
    import time
    site = re.sub(r"^https?://", "", inp["site"]).strip("/")
    if ctx.progress:
        await ctx.progress(f"проверяю {site}")
    out = []
    t = time.monotonic()
    try:
        st, final, html = await ct.fetch(f"https://{site}/")
        out.append(f"Главная: HTTP {st}, итоговый адрес {final}, {time.monotonic() - t:.1f} с, {len(html) // 1024} КБ HTML")
    except Exception as e:
        out.append(f"Главная недоступна: {e}")
    try:
        st, final, _ = await ct.fetch(f"http://{site}/", 200_000)
        out.append(f"http:// → {final} ({'есть редирект на https' if final.startswith('https') else 'НЕТ редиректа на https'})")
    except Exception as e:
        out.append(f"http:// не проверен: {e}")
    sitemaps = []
    try:
        st, _, robots = await ct.fetch(f"https://{site}/robots.txt", 200_000)
        out.append(f"robots.txt: HTTP {st}\n```\n{robots[:2500]}\n```")
        sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
    except Exception as e:
        out.append(f"robots.txt: ошибка {e}")
    for sm in (sitemaps or [f"https://{site}/sitemap.xml"])[:3]:
        try:
            st, _, xml = await ct.fetch(sm, 5_000_000)
            locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml)
            kind = "индекс sitemap-ов" if "<sitemapindex" in xml else "sitemap"
            out.append(f"{sm}: HTTP {st}, {kind}, адресов: {len(locs)}. Примеры: " + ", ".join(locs[:8]))
        except Exception as e:
            out.append(f"{sm}: ошибка {e}")
    return [{"type": "text", "text": "\n".join(out)}]


async def handle(name: str, inp: dict, ctx: Ctx):
    if name == "check_site":
        return await check_site(inp, ctx)
    if name in ct.COMMON_HANDLERS:
        return await ct.COMMON_HANDLERS[name](inp, ctx)
    return [{"type": "text", "text": f"Нет инструмента {name}"}]
