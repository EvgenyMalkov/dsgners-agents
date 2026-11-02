"""HTML-шаблон → PNG + автоматическая проверка вёрстки.

Проверка ловит то, что чаще всего портит визуал:
- текст не влез в блок (сначала пробуем ужать шрифт до data-min);
- текст ближе к краю, чем safe-зона шаблона;
- текст наезжает на фигуры или фото;
- картинка не загрузилась.
"""
import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

from core import config

CHECK_JS = r"""
async ({safe}) => {
  await document.fonts.ready;
  const issues = [];
  const short = s => { s = s.replace(/\s+/g,' ').trim(); return s.length > 40 ? s.slice(0,40)+'…' : s; };
  // допуск: у плотного line-height «хвосты» букв дают пару лишних пикселей scrollHeight
  const over = el => { const tol = Math.max(3, parseFloat(getComputedStyle(el).fontSize) * 0.2);
    return el.scrollHeight > el.clientHeight + tol || el.scrollWidth > el.clientWidth + 2; };

  // 1. Автоподгонка размера шрифта
  for (const el of document.querySelectorAll('[data-fit]')) {
    const min = parseFloat(el.dataset.min || '20');
    let fs = parseFloat(getComputedStyle(el).fontSize), guard = 0;
    while (over(el) && fs > min && guard++ < 200) { fs -= 2; el.style.fontSize = fs + 'px'; }
    if (over(el)) issues.push(`Текст «${short(el.innerText)}» не влезает даже при ${fs}px — сократи его.`);
  }
  for (const el of document.querySelectorAll('[data-box]'))
    if (over(el)) issues.push('Блок с текстом переполнен — сократи заголовок или подзаголовок, либо убери подзаголовок.');

  // 2. Отступы от краёв и наезды на графику (по реальным строкам текста)
  const cv = document.getElementById('canvas').getBoundingClientRect();
  const blockers = [...document.querySelectorAll('.c1,.c2,.r1,.photo')].map(e => e.getBoundingClientRect());
  const hit = (a, b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
  const walker = document.createTreeWalker(document.getElementById('canvas'), NodeFilter.SHOW_TEXT);
  const seen = new Set();
  while (walker.nextNode()) {
    const n = walker.currentNode;
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    const r = document.createRange(); r.selectNodeContents(n);
    // ограничиваем видимой областью элемента (overflow:hidden)
    const box = el.getBoundingClientRect();
    for (const rr of r.getClientRects()) {
      const vis = { left: Math.max(rr.left, box.left), right: Math.min(rr.right, box.right),
                    top: Math.max(rr.top, box.top), bottom: Math.min(rr.bottom, box.bottom) };
      if (vis.right <= vis.left || vis.bottom <= vis.top) continue;
      const m = Math.min(vis.left - cv.left, cv.right - vis.right, vis.top - cv.top, cv.bottom - vis.bottom);
      const key = short(el.innerText);
      if (m < safe && !seen.has('e'+key)) { seen.add('e'+key); issues.push(`«${key}» в ${Math.round(m)}px от края, нужно не меньше ${safe}px.`); }
      if (!el.closest('[data-over]') && blockers.some(b => hit(vis, b)) && !seen.has('h'+key)) { seen.add('h'+key); issues.push(`«${key}» наезжает на фигуру или фото.`); }
    }
  }

  // 3. Картинки
  for (const img of document.images)
    if (!img.complete || img.naturalWidth === 0) issues.push('Картинка не загрузилась: ' + img.getAttribute('src'));
  return issues;
}
"""

_env = Environment(
    loader=FileSystemLoader(str(config.AGENTS / "designer" / "templates")),
    autoescape=select_autoescape(["html"]),
)


def templates_index() -> dict:
    return json.loads((config.AGENTS / "designer" / "templates" / "index.json").read_text("utf-8"))


def design_system() -> dict:
    return json.loads((config.TEAM / "design-system.json").read_text("utf-8"))


_SHORT = re.compile(r"(^|[\s\u00a0])([А-Яа-яЁёA-Za-z0-9]{1,2})\s+", re.M)


def typograf(text: str) -> str:
    """Русская типографика: тире не начинает строку, короткие слова не висят в конце строки."""
    text = re.sub(r"\s+—", "\u00a0—", text)
    for _ in range(2):  # два прохода — для цепочек «и в от»
        text = _SHORT.sub(lambda m: m.group(1) + m.group(2) + "\u00a0", text)
    return text


def _image_src(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith(("http://", "https://", "file://", "data:")):
        return value
    p = Path(value)
    if not p.is_absolute():
        p = config.ROOT / p
    return p.resolve().as_uri()


async def render(template: str, data: dict, out_png: Path) -> list[str]:
    """Рендерит шаблон в PNG, возвращает список найденных проблем (пустой — всё чисто)."""
    idx = templates_index()
    if template not in idx:
        return [f"Нет шаблона «{template}». Есть: {', '.join(idx)}"]
    meta = idx[template]
    W, H = meta["size"]
    ctx = {k: v for k, v in data.items() if v not in (None, "")}
    for k in ("title", "subtitle", "rubric", "cta"):
        if isinstance(ctx.get(k), str):
            ctx[k] = typograf(ctx[k])
    ctx["image"] = _image_src(ctx.get("image"))
    html = _env.get_template(f"{template}.html").render(ds=design_system(), W=W, H=H, **ctx)

    out_png = out_png.resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    html_path = out_png.with_suffix(".html")
    html_path.write_text(html, "utf-8")

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--allow-file-access-from-files"])
        try:
            page = await browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
            await page.goto(html_path.as_uri(), wait_until="networkidle", timeout=30000)
            issues = await page.evaluate(CHECK_JS, {"safe": meta.get("safe", 48)})
            await page.locator("#canvas").screenshot(path=str(out_png))
        finally:
            await browser.close()
    return issues


MOCKUP_CHECK_JS = r"""
async () => {
  await document.fonts.ready;
  const issues = [];
  const vw = document.documentElement.clientWidth;
  if (document.documentElement.scrollWidth > vw + 1)
    issues.push(`Горизонтальная прокрутка: ширина контента ${document.documentElement.scrollWidth}px при экране ${vw}px.`);
  let n = 0;
  for (const el of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (!el.children.length && el.innerText && el.innerText.trim() && (el.scrollWidth > el.clientWidth + 2) &&
        cs.overflow !== 'visible' && cs.textOverflow !== 'ellipsis') {
      if (n++ < 5) issues.push('Текст не помещается в блок: «' + el.innerText.trim().slice(0, 40) + '»');
    }
    const r = el.getBoundingClientRect();
    if (r.width > 0 && (r.right > vw + 1) && cs.position !== 'fixed' && n < 5 && el.innerText && el.innerText.trim() && !el.children.length) {
      n++; issues.push('Элемент вылезает за правый край: «' + el.innerText.trim().slice(0, 40) + '»');
    }
  }
  for (const img of document.images)
    if (!img.complete || img.naturalWidth === 0) issues.push('Картинка не загрузилась: ' + (img.getAttribute('src') || '').slice(0, 80));
  return issues;
}
"""


async def render_html(html: str, out_png: Path, width: int = 1440, max_height: int = 6000) -> tuple[list[str], Path]:
    """Произвольный HTML (макет) → скриншот всей страницы. Возвращает (проблемы, путь к первому экрану)."""
    out_png = out_png.resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    html_path = out_png.with_suffix(".html")
    html_path.write_text(html, "utf-8")
    first = out_png.with_name(out_png.stem + "-screen.jpg")
    errors: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": width, "height": 900 if width > 600 else 844},
                                          device_scale_factor=1)
            page.on("pageerror", lambda e: errors.append(f"Ошибка JS: {str(e)[:120]}"))
            await page.goto(html_path.as_uri(), wait_until="networkidle", timeout=30000)
            issues = await page.evaluate(MOCKUP_CHECK_JS)
            h = await page.evaluate("document.documentElement.scrollHeight")
            await page.screenshot(path=str(first), type="jpeg", quality=85)
            await page.set_viewport_size({"width": width, "height": min(max(h, 400), max_height)})
            await page.screenshot(path=str(out_png), full_page=False)
            if h > max_height:
                issues.append(f"Страница выше {max_height}px — показан только верх.")
        finally:
            await browser.close()
    return issues + errors[:3], first
