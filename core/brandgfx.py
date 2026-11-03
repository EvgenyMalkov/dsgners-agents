"""Фирменная графика Дизайнерс: шейпы с характерной обводкой на цветном блоке.

Правила из гайдбука, которые здесь зашиты:
- обводка у всех шейпов на носителе одной толщины;
- заливка и обводка шейпа — разные цвета, из допустимых пар; фон блока — яркий фирменный (не чёрный/белый);
- шейпы в композиции близки по размеру;
- два приёма: «mix» (наслоение, как на борде) и «blocks» (каждый шейп в своём цветовом блоке);
- фото — в простой форме с обводкой фирменного цвета.
"""
import hashlib
import json
import random
from html import escape

from core import config

_SHAPES = None


def shapes() -> dict:
    global _SHAPES
    if _SHAPES is None:
        _SHAPES = json.loads((config.TEAM / "assets" / "shapes.json").read_text("utf-8"))
    return _SHAPES


def palettes() -> dict:
    return json.loads((config.TEAM / "design-system.json").read_text("utf-8"))["palettes"]


def _svg_shape(name: str, x, y, w, h, fill, stroke, outline, rot=0.0, z=1) -> str:
    sp = shapes()[name]
    style = (f"position:absolute;left:{x:.0f}px;top:{y:.0f}px;width:{w:.0f}px;height:{h:.0f}px;"
             f"transform:rotate({rot:.1f}deg);z-index:{z};overflow:visible")
    par = ' preserveAspectRatio="none"' if name == "zigzag" else ""
    if sp["kind"] == "line":
        inner = sp["width"] * min(w, h) / 200
        if name == "zigzag":
            inner = sp["width"] * w / 200
        body = (f'<path d="{sp["d"]}" fill="none" stroke="{stroke}" stroke-width="{inner + 2 * outline:.1f}" '
                f'stroke-linejoin="{sp["join"]}" stroke-linecap="{"round" if sp["join"] == "round" else "butt"}" '
                f'stroke-miterlimit="10" vector-effect="non-scaling-stroke"/>'
                f'<path d="{sp["d"]}" fill="none" stroke="{fill}" stroke-width="{inner:.1f}" '
                f'stroke-linejoin="{sp["join"]}" stroke-linecap="{"round" if sp["join"] == "round" else "butt"}" '
                f'stroke-miterlimit="10" vector-effect="non-scaling-stroke"/>')
    else:
        rule = ' fill-rule="evenodd"' if sp.get("evenodd") else ""
        body = (f'<path d="{sp["d"]}"{rule} fill="{fill}" stroke="{stroke}" stroke-width="{2 * outline:.1f}" '
                f'stroke-linejoin="round" paint-order="stroke" vector-effect="non-scaling-stroke"/>')
        if sp.get("inner") == "eyes":
            for ex in (78, 122):
                body += (f'<circle cx="{ex}" cy="98" r="17" fill="#FFFFFF" stroke="#000" stroke-width="3"/>'
                         f'<circle cx="{ex + 3}" cy="101" r="8.5" fill="#000"/>')
        elif sp.get("inner"):
            inn = shapes()[sp["inner"]]
            body += (f'<g transform="translate(100 96) scale(.42) translate(-100 -100)">'
                     f'<path d="{inn["d"]}" fill="{stroke}"/></g>')
    return (f'<svg class="shape-el" data-shape="{name}" viewBox="0 0 200 200"{par} style="{style}" '
            f'xmlns="http://www.w3.org/2000/svg">{body}</svg>')


def _pick_motifs(motifs, n, rnd):
    names = [m for m in (motifs or []) if m in shapes() and m != "plus"]
    pool = [k for k in shapes() if k not in ("plus", "speech", "cloudeyes")]
    while len(names) < n:
        c = rnd.choice(pool)
        if c not in names:
            names.append(c)
    return names[:n]


def build(w: int, h: int, *, motifs=None, palette="auto", mode="mix", image=None,
          outline=14, seed="", image_shape="rounded") -> tuple[str, str]:
    """Возвращает (цвет фона блока, html содержимого) для графического блока w×h."""
    rnd = random.Random(int(hashlib.md5(seed.encode()).hexdigest()[:8], 16))
    pals = palettes()
    pname = palette if palette in pals else rnd.choice(list(pals))
    pal = pals[pname]

    if mode == "blocks" and not image:
        horizontal = w >= h * 0.9
        k = 3
        others = [p for p in pals if p != pname]
        rnd.shuffle(others)
        names = _pick_motifs(motifs, k, rnd)
        parts = []
        for i in range(k):
            p = pals[[pname, *others][i]]
            fill, stroke = p["pairs"][i % len(p["pairs"])]
            if horizontal:
                bx, by, bw, bh = round(i * w / k), 0, round((i + 1) * w / k) - round(i * w / k), h
            else:
                bx, by, bw, bh = 0, round(i * h / k), w, round((i + 1) * h / k) - round(i * h / k)
            s = min(bw, bh) * 0.86
            if names[i] == "zigzag":
                sw, sh = bw * 1.04, bh * 1.04
                sx, sy = bx - bw * 0.02, by - bh * 0.02
            else:
                sw = sh = s
                sx, sy = bx + (bw - s) / 2, by + (bh - s) / 2
            parts.append(f'<div style="position:absolute;left:{bx}px;top:{by}px;width:{bw}px;height:{bh}px;'
                         f'background:{p["bg"]};overflow:hidden"></div>')
            inner = _svg_shape(names[i], sx - bx, sy - by, sw, sh, fill, stroke, outline,
                               rot=0 if names[i] in ("zigzag",) else rnd.uniform(-10, 10))
            parts[-1] = parts[-1].replace("></div>", f">{inner}</div>")
        return pal["bg"], "".join(parts)

    # mix: наслоение шейпов, близких по размеру, вдоль длинной стороны блока
    ratio = w / h
    k = sum(1 for m in (motifs or []) if m in shapes())
    cut = bool(image) and image_shape == "cutout"
    if image:
        n = 3 if cut else 2
    elif ratio > 2.2 or ratio < 1 / 2.2:
        n = min(4, max(3, k or 3))
    else:
        n = min(3, max(2, k or 3))
    names = _pick_motifs(motifs, n, rnd)
    pairs = pal["pairs"][:]
    rnd.shuffle(pairs)
    out = []
    horizontal = ratio >= 1
    L, S = (w, h) if horizontal else (h, w)          # длинная и короткая сторона
    size = min(S * 0.98, L / n * 1.3)
    if cut:
        centers = [0.12, 0.5, 0.88]   # шейпы вокруг и позади человека
    elif image:
        centers = [0.1, 0.9]
    elif n == 2:
        centers = [0.22, 0.8]
    else:
        centers = [0.1 + 0.8 * i / (n - 1) for i in range(n)]
    for i, nm in enumerate(names):
        fill, stroke = pairs[i % len(pairs)]
        s_ = size * rnd.uniform(0.9, 1.06)
        c_long = centers[i] * L + rnd.uniform(-0.03, 0.03) * L
        c_short = S / 2 + rnd.uniform(-0.16, 0.16) * S
        if nm == "zigzag":
            zl, zs = s_ * 1.1, S * 1.08          # вдоль длинной стороны — ширина, поперёк — во всю высоту
            if horizontal:
                x, y, sw, sh = c_long - zl / 2, -S * 0.04, zl, zs
            else:
                x, y, sw, sh = -S * 0.04, c_long - zl / 2, zs, zl
            out.append(_svg_shape(nm, x, y, sw, sh, fill, stroke, outline, 0, z=1 + i))
            continue
        cx, cy = (c_long, c_short) if horizontal else (c_short, c_long)
        out.append(_svg_shape(nm, cx - s_ / 2, cy - s_ / 2, s_, s_, fill, stroke, outline,
                              rnd.uniform(-14, 14), z=1 + i))
    if cut and mode == "frame":
        # вариант из макета «Как выйти из (любого) тупика?»: рамка из скруглённых палочек + круг, человек в центре
        out = []
        fill, stroke = pal["pairs"][0]
        f2, s2 = pal["pairs"][1 % len(pal["pairs"])]
        t = max(10, h * 0.055)
        inset = h * 0.07
        sticks = [(w * 0.18, inset, w * 0.64, t), (w * 0.18, h - inset - t, w * 0.64, t),
                  (inset * 1.2, h * 0.2, t, h * 0.6), (w - inset * 1.2 - t, h * 0.2, t, h * 0.6)]
        for (x, y, sw, sh) in sticks:
            out.append(f'<div class="shape-el" style="position:absolute;left:{x:.0f}px;top:{y:.0f}px;width:{sw:.0f}px;height:{sh:.0f}px;'
                       f'border-radius:{t:.0f}px;background:{fill};box-shadow:0 0 0 {outline * 0.7:.0f}px {stroke};z-index:1"></div>')
        cd = h * 0.24
        out.append(f'<div class="shape-el" style="position:absolute;left:{w * 0.66:.0f}px;top:{h * 0.13:.0f}px;width:{cd:.0f}px;height:{cd:.0f}px;'
                   f'border-radius:50%;background:{f2};box-shadow:0 0 0 {outline:.0f}px {s2};z-index:1"></div>')
        out.append(f'<img class="photo cutout" src="{escape(image)}" alt="" style="position:absolute;left:50%;bottom:0;'
                   f'transform:translateX(-50%);height:{h * 0.86:.0f}px;max-width:{w * 0.7:.0f}px;object-fit:contain;'
                   f'object-position:bottom center;z-index:10">')
        return pal["bg"], "".join(out)
    if cut:
        # одно фото человека — обтравка: стоит на нижнем крае блока поверх графики, без рамки (гайдбук «Фото»)
        out.append(f'<img class="photo cutout" src="{escape(image)}" alt="" style="position:absolute;'
                   f'left:50%;bottom:0;transform:translateX(-50%);height:{h * 0.94:.0f}px;max-width:{w * 0.8:.0f}px;'
                   f'object-fit:contain;object-position:bottom center;z-index:10">')
    elif image:
        fill, stroke = pal["pairs"][0]
        ph = h * 0.8
        pw = ph * (1.0 if image_shape == "circle" else 1.25)
        pw = min(pw, w * 0.72)
        radius = "50%" if image_shape == "circle" else f"{outline * 2.5:.0f}px"
        out.append(f'<img class="photo" src="{escape(image)}" alt="" style="position:absolute;'
                   f'left:{(w - pw) / 2:.0f}px;top:{(h - ph) / 2:.0f}px;width:{pw:.0f}px;height:{ph:.0f}px;'
                   f'object-fit:cover;border-radius:{radius};outline:{outline:.0f}px solid {stroke};z-index:10">')
    return pal["bg"], "".join(out)


def font_css() -> str:
    """Фирменный шрифт из team/fonts/: CoFo Sona (по гайдбуку), если нет — CoFo Sans; иначе Golos Text."""
    fdir = config.TEAM / "fonts"
    faces = []
    files = [f for f in sorted(fdir.glob("*")) if f.suffix.lower() in (".woff2", ".woff", ".otf", ".ttf")] if fdir.exists() else []
    sona = [f for f in files if "sona" in f.name.lower()]
    for f in sona or [f for f in files if "cofo" in f.name.lower()]:
        low = f.name.lower()
        weight = 500 if "medium" in low else 400
        faces.append(f"@font-face{{font-family:'CoFo Sona';src:url('{f.resolve().as_uri()}');font-weight:{weight}}}")
    link = "" if faces else ('<link href="https://fonts.googleapis.com/css2?family=Golos+Text:wght@400;500&display=swap" '
                             'rel="stylesheet">')
    return link + "<style>" + "".join(faces) + "</style>"


def corner_shapes(w: int, h: int, *, motifs=None, palette="auto", outline=14, seed="", bg=None) -> str:
    """Шейпы по углам «витрины» (гайдбук → Находки): зигзаг снизу слева, курсор справа, облачко снизу справа."""
    rnd = random.Random(int(hashlib.md5(seed.encode()).hexdigest()[:8], 16))
    pals = palettes()
    pal = pals[palette] if palette in pals else pals[rnd.choice(list(pals))]
    pairs = [p for p in pal["pairs"] if bg is None or p[0].lower() != bg.lower()] or pal["pairs"]
    names = _pick_motifs(motifs or ["zigzag", "cursor", "cloud"], 3, rnd)
    m = min(w, h)
    wide = w > h * 1.2
    # слоты: курсор — справа по центру поверх рамки; зигзаг — снизу слева; остальное — снизу справа (за рамкой)
    slot_right = (w - (0.2 if wide else 0.13) * m, h * (0.5 if wide else 0.42), (0.24 if wide else 0.17) * m)
    slot_bl = (-0.03 * w, h - 0.42 * m, (0.42 if wide else 0.3) * m)
    slot_br = (w - (0.27 if wide else 0.2) * m, h - (0.24 if wide else 0.18) * m, (0.36 if wide else 0.28) * m)
    order = sorted(names, key=lambda n: 0 if n == "cursor" else (1 if n == "zigzag" else 2))
    slots = {}
    free = [slot_right, slot_bl, slot_br]
    for nm in order:
        if nm == "cursor" and slot_right in free:
            slots[nm] = slot_right
        elif nm == "zigzag" and slot_bl in free:
            slots[nm] = slot_bl
        else:
            slots[nm] = [x for x in free if x not in slots.values()][-1]
        free = [x for x in free if x not in slots.values()]
    out = []
    for i, nm in enumerate(names):
        fill, stroke = pairs[i % len(pairs)]
        x, y, sz = slots[nm]
        z = 3 if slots[nm] == slot_right else 1   # только правый элемент заходит поверх рамки
        if nm == "zigzag":
            out.append(_svg_shape(nm, x, y, sz * 0.9, sz * 1.15, fill, stroke, outline, -8, z=z))
        else:
            out.append(_svg_shape(nm, x, y, sz, sz, fill, stroke, outline, rnd.uniform(-12, 12), z=z))
    return "".join(out)


# ---------- обтравка фото человека (rembg, модель u2net_human_seg) ----------
_session = None


def cutout(path: str) -> str | None:
    """Убирает фон с фото человека, обрезает по контуру. Возвращает путь к PNG или None, если не вышло."""
    from pathlib import Path
    src = Path(path)
    if not src.exists():
        return None
    import hashlib as _h
    from PIL import Image
    key = _h.md5(src.read_bytes()).hexdigest()[:16]
    out = config.DATA / "cutouts" / f"{key}.png"
    if out.exists():
        return str(out)
    global _session
    try:
        from rembg import new_session, remove
        if _session is None:
            _session = new_session("u2net_human_seg")
        im = Image.open(src).convert("RGB")
        im.thumbnail((1600, 1600))
        res = remove(im, session=_session, post_process_mask=True)
        bbox = res.getbbox()
        if not bbox:
            return None
        res = res.crop(bbox)
        # человек «стоит» на нижнем крае: если обрезан снизу кадра — ок; иначе всё равно ставим вниз
        out.parent.mkdir(parents=True, exist_ok=True)
        res.save(out, "PNG")
        return str(out)
    except Exception:
        import logging
        logging.exception("cutout failed")
        return None
