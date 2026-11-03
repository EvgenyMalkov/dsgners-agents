"""Полноформатные шаблоны по макетам из Figma (Коммуникационка дизайнерс):

collage — «Наши любимые спикеры»: белый инфоблок сверху, снизу графика с фото людей
          (graphic: hearts — сетка сердец, фото в клетках; bars — столбики, фото в кругах/квадратах с обводкой)
digest  — «Дайджест новостей»: цветной фон, заголовок слева, справа сетка кругов с фото/логотипами
poster  — «Рейтинг / анонс»: цветной фон, крупный заголовок по центру, центральная картинка, бейджи «Топ 1…»
"""
import hashlib
import math
import random
from html import escape

from core import brandgfx, config

PHOTO_STROKES = ["#9B50FF", "#F83939", "#4772FF", "#FEF014", "#FF72FF", "#43C9FE"]


def _rnd(seed: str) -> random.Random:
    return random.Random(int(hashlib.md5(seed.encode()).hexdigest()[:8], 16))


def text_on(bg: str) -> str:
    """Чёрный текст на светлом фоне, белый — на тёмном/насыщенном (как в макетах: синий, фиолетовый, розовый)."""
    h = bg.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#000000" if lum > 0.55 else "#FFFFFF"


def _bg(palette: str | None, default: str) -> str:
    pals = brandgfx.palettes()
    if palette == "black":
        return "#000000"
    return (pals.get(palette) or pals[default])["bg"]


def _photo(src, x, y, d, shape, stroke, outline, z=5, w=None, h=None):
    w = w or d
    h = h or d
    radius = "50%" if shape == "circle" else f"{round(min(w, h) * 0.12)}px"
    return (f'<div class="ph" style="position:absolute;left:{x:.0f}px;top:{y:.0f}px;width:{w:.0f}px;height:{h:.0f}px;'
            f'border-radius:{radius};box-shadow:0 0 0 {outline:.0f}px {stroke};overflow:hidden;background:#fff;z-index:{z}">'
            + (f'<img class="photo" src="{escape(src)}" alt="" style="width:100%;height:100%;object-fit:cover;display:block">'
               if src else "") + "</div>")


def _logo(color: str, width: float, x: float, y: float) -> str:
    svg = (config.TEAM / "assets" / "logo.svg").read_text("utf-8")
    svg = svg.replace("<svg ", '<svg style="width:100%;height:auto;display:block" ', 1)
    return (f'<div style="position:absolute;left:{x:.0f}px;top:{y:.0f}px;width:{width:.0f}px;color:{color};line-height:0;z-index:20">'
            f'{svg}</div>')


def _head(W, H, bg) -> str:
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">{brandgfx.font_css()}<style>'
            f'*{{box-sizing:border-box;margin:0;padding:0}}html,body{{background:#808080}}'
            f'#canvas{{position:relative;overflow:hidden;width:{W}px;height:{H}px;background:{bg};'
            f"font-family:'CoFo Sona','Golos Text',sans-serif;font-weight:400;-webkit-font-smoothing:antialiased}}"
            f'.t{{position:absolute;letter-spacing:-0.025em;line-height:1;overflow:hidden;padding-bottom:.08em;text-wrap:balance}}'
            f'.s{{position:absolute;letter-spacing:-0.01em;line-height:1.08;overflow:hidden}}'
            f'</style></head><body><div id="canvas">')


TAIL = "</div></body></html>"


# ---------------------------------------------------------------- collage
def collage(ctx: dict, W: int, H: int, imgs: list[str | None]) -> str:
    rnd = _rnd(f"{ctx.get('title')}|collage")
    variant = ctx.get("graphic") if ctx.get("graphic") in ("hearts", "bars") else "bars"
    story = H > W * 1.5
    info_h = round(H * (0.33 if story else (0.36 if H > W * 1.1 else 0.4)))
    pad = round(W * 0.045)
    top = 230 if story else pad
    fs = round(W * (0.11 if story else 0.098))
    sub_fs = round(W * 0.03)
    out = [_head(W, H, "#FFFFFF")]
    out.append(f'<div class="t" data-fit data-min="{round(fs * 0.5)}" style="left:{pad}px;top:{top}px;width:{W * 0.78:.0f}px;'
               f'max-height:{info_h - top - pad - sub_fs * 3}px;font-size:{fs}px">{escape(ctx.get("title", ""))}</div>')
    if ctx.get("subtitle"):
        out.append(f'<div class="s" data-fit data-min="{round(sub_fs * 0.75)}" style="left:{pad}px;bottom:{H - info_h + pad * 0.7:.0f}px;'
                   f'width:{W * 0.55:.0f}px;max-height:{sub_fs * 2.3:.0f}px;font-size:{sub_fs}px">{escape(ctx["subtitle"])}</div>')
    note = ctx.get("date") or ctx.get("rubric")
    if note:
        out.append(f'<div class="s" style="right:{pad}px;bottom:{H - info_h + pad * 0.7:.0f}px;max-width:{W * 0.38:.0f}px;'
                   f'text-align:right;max-height:{sub_fs * 2.3:.0f}px;font-size:{sub_fs}px">{escape(note)}</div>')
    gy, gh = info_h, H - info_h
    outline = round(W * 0.011)
    if variant == "hearts":
        bg = _bg(ctx.get("palette"), "blue")
        out.append(f'<div style="position:absolute;left:0;top:{gy}px;width:{W}px;height:{gh}px;background:{bg};overflow:hidden">')
        cols = 6
        cell = W / cols
        rows = math.ceil(gh / cell) + 1
        heart_fill, heart_stroke = ("#F83939", "#FF72FF") if bg != "#F83939" else ("#FF72FF", "#FEF014")
        slots = [(1, 1), (4, 2), (2, 3), (4, 0), (0, 3), (3, 1)]
        slots = [(c, r) for c, r in slots if r * cell + cell * 0.9 < gh][:len(imgs)]
        taken = set(slots)
        for r in range(rows):
            for c in range(cols):
                if (c, r) in taken:
                    continue
                s = cell * 0.96
                out.append(brandgfx._svg_shape("heart", c * cell + (cell - s) / 2, r * cell + (cell - s) / 2 + cell * 0.06,
                                               s, s, heart_fill, heart_stroke, outline * 0.8, 0, z=1))
        for i, ((c, r), src) in enumerate(zip(slots, imgs)):
            d = cell * 0.8
            out.append(_photo(src, c * cell + (cell - d) / 2, r * cell + (cell - d) / 2 + cell * 0.06, d,
                              "circle" if i % 2 else "square", PHOTO_STROKES[(i + 3) % len(PHOTO_STROKES)], outline))
        out.append("</div>")
    else:
        bg = _bg(ctx.get("palette"), "green")
        out.append(f'<div style="position:absolute;left:0;top:{gy}px;width:{W}px;height:{gh}px;background:{bg};overflow:hidden">')
        nb = 9
        bw = W / 15
        bar_fill, bar_stroke = ("#FF72FF", "#43C9FE") if bg not in ("#FF72FF", "#43C9FE") else ("#9B50FF", "#FEF014")
        for i in range(nb):
            x = W * (0.03 + 0.95 * i / (nb - 1)) - bw / 2
            hh = gh * rnd.uniform(0.35, 0.75)
            y = gh * rnd.uniform(0.12, 0.3)
            out.append(f'<div style="position:absolute;left:{x:.0f}px;top:{y:.0f}px;width:{bw:.0f}px;height:{hh:.0f}px;'
                       f'background:{bar_fill};box-shadow:0 0 0 {outline}px {bar_stroke};z-index:1"></div>')
        n = max(1, len(imgs))
        d = min(W * (0.3 if n <= 2 else 0.24), gh * 0.42)
        spots = [(0.37, 0.12), (0.56, 0.36), (0.3, 0.58), (0.74, 0.08), (0.08, 0.3)][:n]
        for i, ((fx, fy), src) in enumerate(zip(spots, imgs)):
            out.append(_photo(src, W * fx, gh * fy, d, "square" if i == 0 else "circle",
                              PHOTO_STROKES[i % len(PHOTO_STROKES)], outline, z=5 + i))
        out.append(brandgfx._svg_shape("speech", W * 0.08, gh * 0.48, d * 0.62, d * 0.62, "#D5F839", "#4772FF", outline, 0, z=4))
        out.append(brandgfx._svg_shape("speech", W * 0.72, gh * 0.28, d * 0.55, d * 0.55, "#9B50FF", "#F83939", outline, 0, z=9))
        out.append("</div>")
    return "".join(out) + TAIL


# ---------------------------------------------------------------- digest
def digest(ctx: dict, W: int, H: int, imgs: list[str | None]) -> str:
    bg = _bg(ctx.get("palette"), "lime")
    fg = text_on(bg)
    wide = W > H * 1.2
    pad = round(H * (0.07 if wide else 0.05))
    fs = round(H * (0.13 if wide else 0.085))
    sub_fs = round(H * (0.04 if wide else 0.028))
    outline = round(H * 0.012)
    out = [_head(W, H, bg)]
    text_w = W * (0.48 if wide else 0.9)
    out.append(f'<div class="t" data-fit data-min="{round(fs * 0.5)}" style="left:{pad}px;top:{pad}px;width:{text_w:.0f}px;'
               f'max-height:{H * (0.62 if wide else 0.3):.0f}px;font-size:{fs}px;color:{fg};z-index:10">{escape(ctx.get("title", ""))}</div>')
    if ctx.get("subtitle"):
        out.append(f'<div class="s" data-fit data-min="{round(sub_fs * 0.75)}" style="left:{pad}px;bottom:{pad}px;width:{text_w:.0f}px;'
                   f'max-height:{sub_fs * 2.3:.0f}px;font-size:{sub_fs}px;color:{fg};z-index:10">{escape(ctx["subtitle"])}</div>')
    # сетка кругов справа (или снизу в квадрате)
    if wide:
        ax, ay, aw, ah, cols, rows = W * 0.5, H * 0.04, W * 0.48, H * 0.92, 3, 2
    else:
        ax, ay, aw, ah, cols, rows = W * 0.06, H * 0.36, W * 0.88, H * 0.6, 3, 2
    d = min(aw / cols, ah / rows) * 1.04
    gx = ax + (aw - d * cols) / 2
    gy = ay + (ah - d * rows) / 2
    cells = [(c, r) for r in range(rows) for c in range(cols)]
    order = [1, 2, 4, 5, 0, 3]                 # первыми заполняются правые/центральные круги
    filled = {cells[k]: src for k, src in zip(order, imgs)}
    bubble_cell = cells[3] if cells[3] not in filled else None
    for (c, r) in cells:
        x, y = gx + c * d, gy + r * d
        if (c, r) in filled:
            out.append(_photo(filled[(c, r)], x + outline, y + outline, d - 2 * outline, "circle", "#FF72FF", outline, z=3))
        elif (c, r) == bubble_cell:
            out.append(brandgfx._svg_shape("speech", x + outline, y + outline, d - 2 * outline, d - 2 * outline,
                                           "#F83939", "#FF72FF", outline, 0, z=3))
        else:
            out.append(f'<div style="position:absolute;left:{x + outline:.0f}px;top:{y + outline:.0f}px;width:{d - 2 * outline:.0f}px;'
                       f'height:{d - 2 * outline:.0f}px;border-radius:50%;background:#F6F0FF;box-shadow:0 0 0 {outline}px #FF72FF;z-index:2"></div>')
    return "".join(out) + TAIL


# ---------------------------------------------------------------- poster
def poster(ctx: dict, W: int, H: int, imgs: list[str | None]) -> str:
    rnd = _rnd(f"{ctx.get('title')}|poster")
    bg = _bg(ctx.get("palette"), "blue")
    fg = text_on(bg)
    pad = round(W * 0.06)
    fs = round(W * 0.085)
    sub_fs = round(W * 0.034)
    outline = round(W * 0.011)
    out = [_head(W, H, bg)]
    out.append(f'<div class="t" data-fit data-min="{round(fs * 0.55)}" style="left:{pad}px;top:{pad}px;width:{W - 2 * pad}px;'
               f'max-height:{fs * 2.3:.0f}px;font-size:{fs}px;color:{fg};text-align:center;z-index:10">{escape(ctx.get("title", ""))}</div>')
    if ctx.get("subtitle"):
        out.append(f'<div class="s" data-fit data-min="{round(sub_fs * 0.75)}" style="left:{W * 0.12:.0f}px;top:{pad + fs * 1.15:.0f}px;'
                   f'width:{W * 0.76:.0f}px;max-height:{sub_fs * 2.3:.0f}px;font-size:{sub_fs}px;color:{fg};text-align:center;z-index:10">'
                   f'{escape(ctx["subtitle"])}</div>')
    cx, cy = W / 2, H * 0.58
    pals = brandgfx.palettes()
    pal = next((p for p in pals.values() if p["bg"] == bg), pals["blue"])
    fill, stroke = pal["pairs"][0]
    main = imgs[0] if imgs else None
    if main:
        mw, mh = W * 0.56, H * 0.5
        if ctx.get("image_shape") == "rounded":   # скриншот/карточка интерфейса — белая карточка с тенью
            out.append(f'<div class="ph" style="position:absolute;left:{cx - mw / 2:.0f}px;top:{cy - mh / 2:.0f}px;width:{mw:.0f}px;'
                       f'height:{mh:.0f}px;border-radius:{W * 0.025:.0f}px;overflow:hidden;background:#fff;z-index:6;'
                       f'box-shadow:0 {W * 0.02:.0f}px {W * 0.05:.0f}px rgba(0,0,0,.25)"><img class="photo" src="{escape(main)}" '
                       f'style="width:100%;height:100%;object-fit:cover;object-position:top;display:block"></div>')
        else:                                      # 3D-объект, обтравка, логотип — без рамки
            out.append(f'<img class="photo" src="{escape(main)}" alt="" style="position:absolute;left:{cx - mw / 2:.0f}px;'
                       f'top:{cy - mh / 2:.0f}px;width:{mw:.0f}px;height:{mh:.0f}px;object-fit:contain;z-index:6">')
        bs = W * 0.62
        out.append(brandgfx._svg_shape(rnd.choice(["star8", "cloud", "blob"]), cx - bs / 2, cy - bs / 2, bs, bs, fill, stroke, outline, 0, z=2))
    else:                                          # без картинки — облачко с глазами, как в «Рейтинге»
        bs = W * 0.62
        out.append(brandgfx._svg_shape("cloudeyes", cx - bs / 2, cy - bs / 2, bs, bs, fill, stroke, outline, 0, z=2))
    badges = (ctx.get("badges") or [])[:4]
    spots = [(0.06, 0.3), (0.74, 0.33), (0.06, 0.64), (0.74, 0.66)]
    for i, (b, (fx, fy)) in enumerate(zip(badges, spots)):
        d = W * 0.17
        x, y = W * fx, H * fy
        st = PHOTO_STROKES[(i + 3) % len(PHOTO_STROKES)]
        src = b.get("image_src")
        if src:
            out.append(_photo(src, x, y, d, "circle", st, outline, z=8))
        elif b.get("text"):
            out.append(f'<div style="position:absolute;left:{x:.0f}px;top:{y:.0f}px;width:{d:.0f}px;height:{d:.0f}px;border-radius:50%;'
                       f'background:#fff;box-shadow:0 0 0 {outline}px {st};z-index:8;display:flex;align-items:center;justify-content:center;'
                       f'font-size:{d * 0.3:.0f}px;letter-spacing:-0.02em">{escape(b["text"][:4])}</div>')
        if b.get("label"):
            out.append(f'<div class="s" style="left:{x + d * 0.45:.0f}px;top:{y + d * 0.78:.0f}px;background:#FFC260;color:#000;'
                       f'font-size:{W * 0.026:.0f}px;padding:{W * 0.008:.0f}px {W * 0.014:.0f}px;border-radius:{W * 0.012:.0f}px;'
                       f'white-space:nowrap;z-index:9;transform:rotate({rnd.uniform(-8, 8):.0f}deg)">{escape(b["label"])}</div>')
    out.append(_logo(fg, W * 0.17, W - pad - W * 0.17, H - pad - W * 0.17 * 21 / 135))
    return "".join(out) + TAIL


BUILDERS = {"collage": collage, "digest": digest, "poster": poster}
