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
  // переполнение считаем по строкам текста, а не по scrollHeight: у шрифтов с высокими асцендерами
  // (CoFo: 1.26em) «чернильная» область строки шире line-height и scrollHeight ложно растёт
  const over = el => {
    const fs = parseFloat(getComputedStyle(el).fontSize) || 16;
    const r = document.createRange(); r.selectNodeContents(el);
    const rs = [...r.getClientRects()].filter(x => x.width > 0);
    const eb = el.getBoundingClientRect();
    const bottom = rs.length ? Math.max(...rs.map(x => x.bottom)) : eb.bottom;
    const lfs = rs.length ? Math.max(...rs.map(x => x.height)) : fs;
    return (bottom - lfs * 0.12 > eb.bottom + 1) || el.scrollWidth > el.clientWidth + 2;
  };

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
  const gfxEl = document.querySelector('.gfx'); const gr = gfxEl ? gfxEl.getBoundingClientRect() : null;
  const clip = r => gr ? {left: Math.max(r.left, gr.left), right: Math.min(r.right, gr.right), top: Math.max(r.top, gr.top), bottom: Math.min(r.bottom, gr.bottom)} : r;
  const blockers = [...document.querySelectorAll('.shape-el,.photo')].map(e => clip(e.getBoundingClientRect())).filter(r => r.right > r.left && r.bottom > r.top);
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


def design_system() -> dict:  # noqa — оставлено для совместимости
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


def _geometry(meta: dict, layout: str) -> dict:
    W, H = meta["size"]
    g = meta["geo"]
    out = {"geo": g}
    if g["split"] == "h":  # инфоблок слева/справа
        iw = g["info"]
        left = layout != "info-right"
        out.update(ix=0 if left else W - iw, iy=0, iw=iw, ih=H, gx=iw if left else 0, gy=0, gw=W - iw, gh=H,
                   ipt=g["pad"], ipb=g["pad"])
    else:
        ih = g["info"]
        top = layout == "info-top"
        out.update(ix=0, iy=0 if top else H - ih, iw=W, ih=ih, gx=0, gy=ih if top else 0, gw=W, gh=H - ih)
        st, sb = g.get("safe_top", 0), g.get("safe_bottom", 0)
        out.update(ipt=max(g["pad"], st if top else 0) if top else g["pad"],
                   ipb=g["pad"] if top else max(g["pad"], sb))
    return out


async def render(template: str, data: dict, out_png: Path) -> list[str]:
    """Рендерит шаблон в PNG, возвращает список найденных проблем (пустой — всё чисто)."""
    from core import brandgfx

    idx = templates_index()
    if template not in idx or template.startswith("_"):
        return [f"Нет шаблона «{template}». Есть: {', '.join(k for k in idx if not k.startswith('_'))}"]
    meta = idx[template]
    W, H = meta["size"]
    ctx = {k: v for k, v in data.items() if v not in (None, "", [])}
    if meta.get("kind") == "showcase":
        return await _render_showcase(template, meta, ctx, out_png)
    if meta.get("kind") in ("collage", "digest", "poster"):
        return await _render_layout(meta, ctx, out_png)
    for k in ("title", "subtitle", "rubric", "date", "author"):
        if isinstance(ctx.get(k), str):
            ctx[k] = typograf(ctx[k])
    if isinstance(ctx.get("items"), list):
        ctx["items"] = [typograf(str(i)) for i in ctx["items"] if str(i).strip()]
    tb = ctx.get("table")
    if isinstance(tb, dict):
        ctx["table"] = {"columns": [typograf(str(c)) for c in tb.get("columns", [])][:4],
                        "rows": [[typograf(str(v)) for v in r] for r in tb.get("rows", [])][:6]}
    raw_image = ctx.pop("image", None)
    warn = []
    if raw_image and ctx.get("image_shape") == "cutout" and not str(raw_image).startswith(("http", "data:")):
        import asyncio
        p_ = Path(raw_image) if Path(raw_image).is_absolute() else config.ROOT / raw_image
        cut = await asyncio.to_thread(brandgfx.cutout, str(p_))
        if cut:
            raw_image = cut
        else:
            ctx["image_shape"] = "circle"
            warn.append("Обтравка не получилась (не нашёл человека на фото) — поставил фото в круг с обводкой.")
    image = _image_src(raw_image)
    ctx["author_photo"] = _image_src(ctx.get("author_photo"))
    geo = _geometry(meta, ctx.get("layout", "info-left" if meta["geo"]["split"] == "h" else ""))
    motifs = ctx.get("motifs")
    if isinstance(motifs, str):
        motifs = [m.strip() for m in motifs.replace(";", ",").split(",") if m.strip()]
    gbg, gfx = brandgfx.build(geo["gw"], geo["gh"], motifs=motifs, palette=ctx.get("palette", "auto"),
                              mode=ctx.get("graphic", "mix"), image=image, outline=meta["geo"]["outline"],
                              seed=f"{ctx.get('title', '')}|{template}|{ctx.get('palette', '')}",
                              image_shape=ctx.get("image_shape", "rounded"))
    html = _env.get_template(f"{template}.html").render(
        W=W, H=H, gfx=gfx, gbg=gbg, font_css=brandgfx.font_css(),
        logo_svg=(config.TEAM / "assets" / "logo.svg").read_text("utf-8"),
        tag_bg=ctx.get("tag_color") or "#8CFF72", **geo, **{k: v for k, v in ctx.items() if k not in geo})

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
    return warn + issues


async def _render_layout(meta: dict, ctx: dict, out_png: Path) -> list[str]:
    import asyncio

    from core import brandgfx, layouts
    W, H = meta.get("formats", {}).get(ctx.get("format", ""), meta["size"])
    for k in ("title", "subtitle", "rubric", "date"):
        if isinstance(ctx.get(k), str):
            ctx[k] = typograf(ctx[k])
    raw = [i for i in (ctx.get("images") or ([ctx["image"]] if ctx.get("image") else [])) if i][:6]
    warn = []
    if ctx.get("image_shape") == "cutout" and raw:
        p_ = Path(raw[0]) if Path(raw[0]).is_absolute() else config.ROOT / raw[0]
        cut = await asyncio.to_thread(brandgfx.cutout, str(p_))
        if cut:
            raw[0] = cut
        else:
            warn.append("Обтравка не получилась — картинка стоит как есть.")
    imgs = [_image_src(i) for i in raw]
    for b in ctx.get("badges") or []:
        if isinstance(b, dict) and b.get("image"):
            b["image_src"] = _image_src(b["image"])
    html = layouts.BUILDERS[meta["kind"]](ctx, W, H, imgs)
    return warn + await _shoot(html, W, H, out_png, meta.get("safe", 40))


async def _shoot(html: str, W: int, H: int, out_png: Path, safe: int) -> list[str]:
    out_png = out_png.resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    html_path = out_png.with_suffix(".html")
    html_path.write_text(html, "utf-8")
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--allow-file-access-from-files"])
        try:
            page = await browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
            await page.goto(html_path.as_uri(), wait_until="networkidle", timeout=30000)
            issues = await page.evaluate(CHECK_JS, {"safe": safe})
            await page.locator("#canvas").screenshot(path=str(out_png))
        finally:
            await browser.close()
    # фото и шейпы намеренно лежат друг на друге — проверяем только текст против краёв и переполнения
    return [i for i in issues if "наезжает" not in i]


async def _render_showcase(template: str, meta: dict, ctx: dict, out_png: Path) -> list[str]:
    """Витрина скриншотов по гайдбуку («Находки»): фон, логотип, экран(ы) в рамке, шейпы по углам."""
    from core import brandgfx
    fmt = ctx.get("format", "16:9")
    W, H = meta.get("formats", {}).get(fmt, meta["size"])
    pals = brandgfx.palettes()
    pname = ctx.get("palette") or "lime"
    bg = "#000000" if pname == "black" else (pals.get(pname) or pals["lime"])["bg"]
    wide = W > H * 1.2
    stroke = max(6, round((18 / 1920 if wide else 14 / 1080) * W))
    phones = template == "showcase-phones"
    issues: list[str] = []
    if phones:
        imgs = [i for i in (ctx.get("images") or ([ctx["image"]] if ctx.get("image") else [])) if i][:3 if wide else 2]
    else:
        imgs = [ctx["image"]] if ctx.get("image") else []
    if not imgs:
        issues.append("Нет картинки: для витрины нужен image (или images для showcase-phones).")
    srcs = [_image_src(i) for i in imgs] or [None]
    show_logo = not (phones and wide)
    logo_w = round(W * (0.253 if wide else (0.36 if H <= W * 1.1 else 0.42)))
    logo_h = round(logo_w * 21 / 135)
    top_logo = round(H * 0.067) if H <= W * 1.1 else 230
    cap_fs = round(W * (0.03 if wide else 0.045))
    gap = round(H * (0.06 if wide else 0.04))
    caption = typograf(ctx["caption"]) if isinstance(ctx.get("caption"), str) else ""
    y0 = (top_logo + logo_h + gap) if show_logo else round(H * 0.06)
    if caption:
        y0 += round(cap_fs * 1.5)
    frames = []
    if phones:
        n = len(srcs)
        fw = round(W * (0.265 if wide else 0.4))
        fh = round(H - y0 - H * (0.07 if wide else 0.0)) if wide else H
        space = (W - n * fw) / (n + 1) if wide else (W - n * fw) / (n + 1)
        for i, src in enumerate(srcs):
            frames.append({"x": round(space + i * (fw + space)), "y": y0, "w": fw, "h": fh + (0 if wide else 200),
                           "r": round(fw * 0.1), "src": src})
    else:
        fx = round(W * (0.125 if wide else 0.083))
        frames.append({"x": fx, "y": y0, "w": W - 2 * fx, "h": H, "r": round(W * 0.025), "src": srcs[0]})
    shapes = brandgfx.corner_shapes(W, H, motifs=ctx.get("motifs"), palette=ctx.get("palette") if ctx.get("palette") in pals else "pink",
                                    outline=round(stroke * 0.8), seed=f"{caption}|{template}|{imgs}", bg=bg)
    html = _env.get_template(f"{template}.html").render(
        W=W, H=H, bg=bg, frames=frames, shapes=shapes, caption=caption, stroke=stroke,
        logo_svg=(config.TEAM / "assets" / "logo.svg").read_text("utf-8") if show_logo else "",
        logo_w=logo_w, logo_h=logo_h, top_logo=top_logo, cap_fs=cap_fs, gap=gap, font_css=brandgfx.font_css())
    if not show_logo:
        html = html.replace('<div class="logo"></div>', "")
    out_png = out_png.resolve()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    html_path = out_png.with_suffix(".html")
    html_path.write_text(html, "utf-8")
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--allow-file-access-from-files"])
        try:
            page = await browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
            await page.goto(html_path.as_uri(), wait_until="networkidle", timeout=30000)
            issues += await page.evaluate(CHECK_JS, {"safe": meta.get("safe", 30)})
            await page.locator("#canvas").screenshot(path=str(out_png))
        finally:
            await browser.close()
    # шейпы по углам перекрывают рамку экрана намеренно — это не ошибка
    return [i for i in issues if "наезжает" not in i]


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
        browser = await p.chromium.launch(args=["--allow-file-access-from-files"])
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
