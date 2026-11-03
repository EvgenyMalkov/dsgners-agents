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
    return (f'<!--logo--><div data-logo style="position:absolute;left:{x:.0f}px;top:{y:.0f}px;width:{width:.0f}px;color:{color};'
            f'line-height:0;z-index:20">{svg}</div><!--/logo-->')


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


# ---------------------------------------------------------------- promo («креатив 16»)
def _card(src, x, y, w, h, rot, W, z=6) -> str:
    """Карточка интерфейса: белая, скруглённая, с мягкой тенью, картинка — от верха."""
    return (f'<div class="ph" style="position:absolute;left:{x:.0f}px;top:{y:.0f}px;width:{w:.0f}px;height:{h:.0f}px;'
            f'transform:rotate({rot:.1f}deg);border-radius:{W * 0.016:.0f}px;overflow:hidden;background:#fff;z-index:{z};'
            f'box-shadow:0 {W * 0.01:.0f}px {W * 0.03:.0f}px rgba(0,0,0,.18)">'
            + (f'<img class="photo" src="{escape(src)}" alt="" style="width:100%;height:100%;object-fit:cover;'
               f'object-position:top center;display:block">' if src else "") + "</div>")


def promo(ctx: dict, W: int, H: int, imgs: list[str | None]) -> str:
    """Слева светлая панель: заголовок и логотип; справа яркая графика и 1–2 карточки интерфейса внахлёст."""
    rnd = _rnd(f"{ctx.get('title')}|promo")
    bg = _bg(ctx.get("palette"), "pink")
    wide = W > H * 1.2
    lw = W * (0.37 if wide else 1.0)
    lh = H if wide else H * 0.36
    pad = W * (0.045 if wide else 0.06)
    fs = round(W * (0.056 if wide else 0.09))
    outline = round(min(W, H) * 0.014)
    out = [_head(W, H, "#F1F1F3")]
    logo_w = lw * 0.6 if wide else W * 0.32
    logo_h = logo_w * 21 / 135
    logo_top = (H if wide else lh) - pad - logo_h
    # с логотипом заголовку оставляем место до него с зазором в 1.5 высоты логотипа, без логотипа — вся панель
    t_max = (logo_top - logo_h * 1.5 - pad) if not ctx.get("hide_logo") else ((H if wide else lh) - 2 * pad)
    out.append(f'<div class="t" data-fit data-min="{round(fs * 0.5)}" style="left:{pad:.0f}px;top:{pad:.0f}px;width:{lw - 2 * pad:.0f}px;'
               f'max-height:{t_max:.0f}px;font-size:{fs}px">{escape(ctx.get("title", ""))}</div>')
    out.append(_logo("#000000", logo_w, pad, logo_top))
    gx, gy, gw, gh = (lw, 0, W - lw, H) if wide else (0, lh, W, H - lh)
    out.append(f'<div style="position:absolute;left:{gx:.0f}px;top:{gy:.0f}px;width:{gw:.0f}px;height:{gh:.0f}px;background:{bg};overflow:hidden">')
    pals = brandgfx.palettes()
    pal = next((p for p in pals.values() if p["bg"] == bg), pals["pink"])
    pairs = [p for p in pal["pairs"]] + [["#F83939", "#FEF014"], ["#FEF014", "#C7B3FF"]]
    names = (ctx.get("motifs") or ["squiggle", "heart", "zigzag", "stick"])[:4]
    slots = [(-0.06, -0.08, 0.5), (0.68, 0.06, 0.34), (0.66, 0.55, 0.42), (-0.04, 0.62, 0.32)]
    m = min(gw, gh)
    for i, (nm, (fx, fy, fsz)) in enumerate(zip(names, slots)):
        if nm not in brandgfx.shapes():
            continue
        fill, stroke = pairs[i % len(pairs)]
        sz = m * fsz * 1.4
        if nm == "zigzag":
            out.append(brandgfx._svg_shape(nm, gw * fx, gh * fy, sz * 0.75, sz, fill, stroke, outline, -18, z=1))
        else:
            out.append(brandgfx._svg_shape(nm, gw * fx, gh * fy, sz, sz, fill, stroke, outline, rnd.uniform(-10, 10), z=1))
    cw = gw * (0.38 if wide else 0.42)
    if len(imgs) >= 2:
        out.append(_card(imgs[0], gw * 0.3, gh * 0.06, cw, gh * 0.4, 0, W, z=5))
        out.append(_card(imgs[1], gw * 0.2, gh * 0.44, cw * 1.08, gh * 0.62, -3, W, z=6))
    elif imgs:
        out.append(_card(imgs[0], gw * 0.24, gh * 0.08, cw * 1.25, gh * 0.95, -2.5, W, z=6))
    out.append("</div>")
    return "".join(out) + TAIL


# ---------------------------------------------------------------- promo-duo («креатив 9»)
PILL_COLORS = [("#43C9FE", "#8CFF72"), ("#FF72FF", "#4772FF"), ("#4772FF", "#43C9FE"), ("#F83939", "#FF72FF"), ("#C7B3FF", "#FF72FF")]


def promo_duo(ctx: dict, W: int, H: int, imgs: list[str | None]) -> str:
    """Две скруглённые панели: синяя — логотип, шейпы, заголовок; лаймовая — карточка интерфейса и плашки-глаголы."""
    rnd = _rnd(f"{ctx.get('title')}|duo")
    bgL = _bg(ctx.get("palette"), "blue")
    bgR = "#D5F839" if bgL != "#D5F839" else "#4772FF"
    wide = W > H * 1.2
    gap = W * 0.012
    if wide:
        pw, ph = (W - 3 * gap) / 2, H - 2 * gap
        L, R = (gap, gap), (2 * gap + pw, gap)
    else:
        pw, ph = W - 2 * gap, (H - 3 * gap) / 2
        L, R = (gap, gap), (gap, 2 * gap + ph)
    rad = min(pw, ph) * 0.06
    fg = text_on(bgL)
    outline = round(min(W, H) * 0.012)
    out = [_head(W, H, "#FFFFFF")]
    out.append(f'<div style="position:absolute;left:{L[0]:.0f}px;top:{L[1]:.0f}px;width:{pw:.0f}px;height:{ph:.0f}px;'
               f'border-radius:{rad:.0f}px;background:{bgL};overflow:hidden">')
    pals = brandgfx.palettes()
    pal = next((p for p in pals.values() if p["bg"] == bgL), pals["blue"])
    fill, stroke = pal["pairs"][0]
    m = min(pw, ph)
    out.append(brandgfx._svg_shape("cloud", -m * 0.12, m * 0.18, m * 0.4, m * 0.4, fill, stroke, outline, 0, z=1))
    out.append(brandgfx._svg_shape("cursor", m * 0.22, m * 0.42, m * 0.2, m * 0.2, pal["pairs"][1][0], pal["pairs"][1][1], outline, 8, z=2))
    out.append(brandgfx._svg_shape("zigzag", pw * 0.6, -ph * 0.04, pw * 0.42, ph * 0.72, fill, pal["pairs"][1][0], outline, -12, z=1))
    pad = m * 0.07
    out.append(_logo(fg, pw * 0.34, pad, pad))
    fs = round(m * 0.1)
    out.append(f'<div class="t" data-fit data-min="{round(fs * 0.5)}" style="left:{pad:.0f}px;bottom:{pad:.0f}px;width:{pw - 2 * pad:.0f}px;'
               f'max-height:{ph * 0.36:.0f}px;font-size:{fs}px;color:{fg};z-index:5">{escape(ctx.get("title", ""))}</div>')
    out.append("</div>")
    out.append(f'<div style="position:absolute;left:{R[0]:.0f}px;top:{R[1]:.0f}px;width:{pw:.0f}px;height:{ph:.0f}px;'
               f'border-radius:{rad:.0f}px;background:{bgR};overflow:hidden">')
    cw, ch = pw * 0.48, ph * 0.72
    src = imgs[0] if imgs else None
    out.append(f'<div class="ph" style="position:absolute;left:{(pw - cw) / 2:.0f}px;top:{(ph - ch) / 2:.0f}px;width:{cw:.0f}px;'
               f'height:{ch:.0f}px;border-radius:{rad * 0.6:.0f}px;background:#fff;border:{outline:.0f}px solid #fff;overflow:hidden;z-index:3">'
               + (f'<img class="photo" src="{escape(src)}" alt="" style="width:100%;height:100%;object-fit:cover;object-position:top;'
                  f'display:block;border-radius:{rad * 0.4:.0f}px">' if src else "") + "</div>")
    tags = [t for t in (ctx.get("tags") or ["Публикуй", "Читай", "Обсуждай", "Вдохновляйся", "Спрашивай"]) if t][:5]
    # (сторона, отступ от края в долях ширины, top, поворот): плашка может уходить за край,
    # но текст внутри неё — нет (поля плашки 0.8em больше заступа)
    spots = [("r", 0.06, 0.06, -8), ("l", -0.03, 0.24, 4), ("r", -0.03, 0.38, 6), ("r", -0.03, 0.68, -7), ("l", -0.03, 0.84, 9)]
    pfs = round(m * 0.065)
    for i, (t, (side, fx, fy, rot)) in enumerate(zip(tags, spots)):
        pb, ps = PILL_COLORS[i % len(PILL_COLORS)]
        pos = f"left:{pw * fx:.0f}px" if side == "l" else f"right:{pw * fx:.0f}px"
        out.append(f'<div class="s" data-over style="{pos};top:{ph * fy:.0f}px;font-size:{pfs}px;padding:{pfs * 0.35:.0f}px {pfs * 0.8:.0f}px;'
                   f'border-radius:999px;background:{pb};color:{text_on(pb)};box-shadow:0 0 0 {outline:.0f}px {ps};white-space:nowrap;'
                   f'transform:rotate({rot + rnd.uniform(-2, 2):.0f}deg);z-index:6;overflow:visible">{escape(t)}</div>')
    out.append("</div>")
    return "".join(out) + TAIL


BUILDERS = {"collage": collage, "digest": digest, "poster": poster, "promo": promo, "promo-duo": promo_duo}
