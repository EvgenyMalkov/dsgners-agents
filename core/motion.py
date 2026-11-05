"""Ролики дизайнера: живой интерфейс (прототип HTML или страница сайта) в мокапе телефона на фирменной подложке.

Как добиваемся качества (правила владельца):
- плавность: анимации на странице замедляются в RATE раз (CDP Animation.setPlaybackRate), кадры снимаются часто,
  потом склеиваются в реальном времени → MP4 50 fps, GIF 30 fps;
- цвета: MP4 кодируется в bt709 с правильной разметкой, GIF — с полной палитрой (без «выцветания»);
- чёткость: кадр рендерится в 2x и ужимается до формата;
- крупность: приближается не мокап, а страница ВНУТРИ экрана — телефон стоит на месте, `zoom` задаёт масштаб страницы,
  `focus` — элемент (например навбар), который ставится в точку кадра `focus_at`;
- цикл: ролик должен заканчиваться в том же состоянии, что начался (проверяем первый и последний кадр).
"""
import asyncio
import hashlib
import json
import math
import random
import shutil
import subprocess
import time
from html import escape
from pathlib import Path

from core import brandgfx, config

FORMATS = {"4:5": (1080, 1350), "1:1": (1080, 1080), "9:16": (1080, 1920), "16:9": (1920, 1080)}
UI = config.TEAM / "ui"
FONTS = config.TEAM / "fonts" / "inter"


def _font_faces() -> str:
    """Inter / Inter Display из репозитория — как на сайте (не зависим от шрифтов сервера)."""
    out = []
    for fam, pre in (("Inter Display", "InterDisplay"), ("Inter", "Inter")):
        for w, st in ((400, "Regular"), (500, "Medium"), (600, "SemiBold")):
            f = FONTS / f"{pre}-{st}.otf"
            if f.exists():
                out.append(f"@font-face{{font-family:'{fam}';src:url('{f.resolve().as_uri()}');font-weight:{w}}}")
    return "".join(out)


def site_header_html() -> str:
    """Реальная мобильная шапка dsgners (меню, логотип, поиск, профиль) — из Figma «[Develop] DSGNERS 3.0»."""
    svg = (UI / "header-mobile.svg").read_text("utf-8")
    return f'<div class="dsg-hdr" style="position:sticky;top:0;z-index:50;height:64px;line-height:0;background:#F2F3F7">{svg}</div>'


def article_html(a: dict) -> str:
    """Верх статьи как на сайте: автор, заголовок, абзацы, обложка. Размеры и шрифты — из макета статьи в Figma."""
    from core.render import _image_src
    author = escape(a.get("author") or "Автор")
    ini = "".join(w[0] for w in author.split()[:2]).upper()
    paras = [escape(p) for p in (a.get("text") or [])][:6]
    cover = _image_src(a["cover"]) if a.get("cover") else None
    body = []
    for i, p in enumerate(paras):
        body.append(f'<p class="dsg-p">{p}</p>')
        if i == 0 and cover:
            body.append(f'<img class="dsg-img" src="{cover}" alt="">')
    return (f'<div class="dsg-art"><div class="dsg-author"><span class="dsg-ava">{ini}</span><div class="dsg-who"><b>{author}</b>'
            f'<span>{escape(a.get("meta") or "Статья · 28.10")}</span></div></div>'
            f'<h1 class="dsg-title">{escape(a.get("title") or "")}</h1>{"".join(body)}</div>')


ARTICLE_CSS = """
.dsg-art{display:flex;flex-direction:column;gap:16px;font-family:'Inter Display','Inter',sans-serif;color:#292C32}
.dsg-author{display:flex;align-items:center;gap:12px}
.dsg-ava{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;background:#E3E9FF;color:#3654F5;font:600 14px 'Inter Display','Inter'}
.dsg-who{display:flex;flex-direction:column}.dsg-who b{font-weight:500;font-size:16px;line-height:1.45}
.dsg-who span{font-size:13px;line-height:1.45;color:#676E7E}
.dsg-title{font:600 24px/1.3 'Inter Display','Inter';margin:4px 0 0;letter-spacing:0}
.dsg-p{font:400 16px/1.5 'Inter Display','Inter';margin:0}
.dsg-img{width:100%;border-radius:16px;display:block}
.dsg-card{background:#fff;border-radius:24px;padding:20px;margin:12px 0}
.dsg-tap{position:fixed;width:46px;height:46px;margin:-23px 0 0 -23px;border-radius:50%;background:rgba(255,255,255,.35);
  box-shadow:0 0 0 2px rgba(41,44,50,.18);pointer-events:none;z-index:2147483646;animation:dsgtap .55s ease-out forwards}
@keyframes dsgtap{from{transform:scale(.5);opacity:1}to{transform:scale(1.35);opacity:0}}
html{scrollbar-width:none}::-webkit-scrollbar{display:none}
"""


def _resolve(source: str) -> str:
    if source.startswith(("http://", "https://", "file://")):
        return source
    p = Path(source)
    if not p.is_absolute():
        for base in (config.ROOT, config.OUTBOX, config.DATA):
            if (base / source).exists():
                p = base / source
                break
    if not p.exists():
        raise FileNotFoundError(f"Нет файла {source}")
    return p.resolve().as_uri()


# ---------------- подложка ----------------

def _rects_overlap(a, b, pad=0):
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0] or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def backdrop(W, H, palette, motifs, caption_box, phone_box, seed) -> str:
    """Шейпы бренда по свободным местам: не наезжают на подпись, за телефоном — можно (он сверху)."""
    pals = brandgfx.palettes()
    pal = pals.get(palette) or pals["violet"]
    rnd = random.Random(int(hashlib.md5(seed.encode()).hexdigest()[:8], 16))
    pairs = [p for p in pal["pairs"] if p[0].lower() != pal["bg"].lower()] or pal["pairs"]
    names = [m for m in (motifs or []) if m in brandgfx.shapes()] or ["zigzag", "heart", "cloud", "star8"]
    m = min(W, H)
    s = m * 0.26
    slots = []
    if caption_box:   # по бокам от подписи и под ней — так шейпы не задевают текст
        cx0, cy0, cx1, cy1 = caption_box
        mid = (cy0 + cy1) / 2 - s / 2
        slots += [(min(-0.05 * s, cx0 - 28 - s), mid + s * 0.25), (max(W - 0.95 * s, cx1 + 28), mid - s * 0.15)]
        if H - cy1 > s * 0.6:
            slots += [(W * 0.5 - s * 1.6, cy1 + 30), (W * 0.5 + s * 0.6, cy1 + 30)]
    slots += [(-0.06 * s, H - 1.0 * s), (W - 0.95 * s, H - 0.95 * s), (-0.1 * s, 0.05 * s), (W - 0.9 * s, 0.08 * s),
              (-0.25 * s, H * 0.45), (W - 0.75 * s, H * 0.5)]
    head, tail = slots[:4], slots[4:]
    rnd.shuffle(tail)
    slots = head + tail
    out, used = [], []
    outline = round(m * 0.011)
    for x, y in slots:
        if len(out) >= min(4, len(names) + 1):
            break
        sz = s * rnd.uniform(0.85, 1.05)
        r = (x, y, x + sz, y + sz)
        if min(r[2], W) - max(r[0], 0) < sz * 0.4 or min(r[3], H) - max(r[1], 0) < sz * 0.4:
            continue   # почти за кадром
        if caption_box and _rects_overlap(r, caption_box, pad=24):
            continue
        if any(_rects_overlap(r, u, pad=10) for u in used):
            continue
        # целиком спрятанный за телефоном шейп не нужен
        if phone_box and r[0] >= phone_box[0] and r[2] <= phone_box[2] and r[1] >= phone_box[1] and r[3] <= phone_box[3]:
            continue
        nm = names[len(out) % len(names)]
        fill, stroke = pairs[len(out) % len(pairs)]
        w, h = (sz * 0.8, sz * 1.1) if nm == "zigzag" else (sz, sz)
        out.append(brandgfx._svg_shape(nm, x, y, w, h, fill, stroke, outline, rnd.uniform(-14, 14), z=1))
        used.append(r)
    return "".join(out)


# ---------------- запись ----------------

def _layout(spec: dict, W: int, H: int):
    VW, VH = spec.get("viewport") or (375, 812)
    zoom = float(spec.get("zoom") or 0)
    cap = (spec.get("caption") or "").strip()
    if not zoom:   # весь телефон в кадре
        zoom = min((H * (0.78 if cap else 0.9)) / VH, (W * 0.8) / VW)
    B = max(10, round(7.5 * zoom))
    SW, SH = VW * zoom, VH * zoom
    return VW, VH, zoom, B, SW, SH, cap


async def record(spec: dict, out_dir: Path, name: str, progress=None) -> dict:
    """Записать ролик. Возвращает {mp4, gif, sheet, issues, seconds}."""
    from playwright.async_api import async_playwright
    from core.layouts import text_on
    if not shutil.which("ffmpeg"):
        return {"issues": ["На сервере нет ffmpeg — ролик не собрать. Нужно обновление сервера (ставится автоматически)."]}
    W, H = FORMATS.get(spec.get("format") or "4:5", FORMATS["4:5"])
    VW, VH, K, B, SW, SH, cap = _layout(spec, W, H)
    src = _resolve(spec["source"])
    work = out_dir / f".{name}-frames"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    issues: list[str] = []
    pal = spec.get("palette") or "violet"
    bg = (brandgfx.palettes().get(pal) or brandgfx.palettes()["violet"])["bg"]

    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--allow-file-access-from-files"])
        try:
            # 1) загружаем страницу отдельно, чтобы узнать, где фокус (навбар и т.п.), и подготовить её
            pg = await b.new_page(viewport={"width": W, "height": H}, device_scale_factor=2)
            stage = work / "stage.html"
            stage.write_text(f"""<!doctype html><html><head><meta charset=utf-8>{brandgfx.font_css()}<style>
*{{box-sizing:border-box;margin:0;padding:0}} html,body{{background:#808080}}
#canvas{{position:relative;overflow:hidden;width:{W}px;height:{H}px;background:{bg};font-family:'CoFo Sona','Golos Text',sans-serif}}
#bd{{position:absolute;inset:0;z-index:1}}
.cap{{position:absolute;left:6%;right:6%;text-align:center;color:{text_on(bg)};
  letter-spacing:-0.03em;line-height:1;z-index:4;white-space:pre-line}}
.phone{{position:absolute;background:#0B0B0D;padding:{B}px;z-index:3;box-shadow:0 30px 60px rgba(0,0,0,.18)}}
.scr{{width:{SW:.1f}px;height:{SH:.1f}px;overflow:hidden;position:relative;background:#F2F3F7}}
iframe{{border:0;width:{VW}px;height:{VH}px;transform:scale({K});transform-origin:0 0;display:block;position:absolute;left:0;top:0}}
</style></head><body><div id=canvas><div id=bd></div><div class=cap id=cap></div>
<div class=phone id=phone><div class=scr><iframe id=f src="{src}"></iframe></div></div></div></body></html>""", "utf-8")
            await pg.goto(stage.as_uri(), wait_until="load", timeout=45000)
            await pg.wait_for_timeout(1200)
            fr = next((x for x in pg.frames if x != pg.main_frame), None)
            if fr is None:
                raise RuntimeError("страница не загрузилась")
            if spec.get("theme") in ("light", "dark"):
                await fr.evaluate(f"document.documentElement.dataset.theme='{spec['theme']}'")
            if src.startswith("file:"):
                await fr.add_style_tag(content=_font_faces())
            await fr.add_style_tag(content=ARTICLE_CSS + (spec.get("css") or ""))
            if spec.get("article"):
                a = spec["article"]
                before = a.get("before")
                await fr.evaluate("""([html, before, card]) => {
                    const box = document.createElement('div'); box.innerHTML = html; const node = box.firstElementChild;
                    const t = before && document.querySelector(before);
                    if (t) t.before(node); else { const c = document.createElement('div'); c.className = card ? 'dsg-card' : '';
                      c.append(node); document.body.prepend(c); }
                }""", [article_html(a), before, True])
            if spec.get("header", src.startswith("file:")):
                await fr.evaluate("h => document.body.insertAdjacentHTML('afterbegin', h)", site_header_html())
            if spec.get("js"):
                await fr.evaluate(f"() => {{ {spec['js']} }}")
            await fr.evaluate("scrollTo(0, 0)")
            await pg.wait_for_timeout(500)

            # 2) раскладка кадра: фокус-элемент → точка focus_at; иначе телефон по центру свободной зоны
            fx, fy = spec.get("focus_at") or (0.5, 0.42)
            fc = None
            if spec.get("focus"):
                fc = await fr.evaluate("s => { const e = document.querySelector(s); if (!e) return null; const r = e.getBoundingClientRect();"
                                       " return [r.x + r.width / 2, r.y + r.height / 2] }", spec["focus"])
                if fc is None:
                    issues.append(f"focus: не нашёл элемент {spec['focus']}")
            if fc:
                px = W * fx - B - fc[0] * K
                py = H * fy - B - fc[1] * K
            else:
                px = (W - SW) / 2 - B
                py = (H - SH) / 2 - B + (H * 0.06 if cap and spec.get("caption_pos") == "top" else 0)
            phone = (px, py, px + SW + 2 * B, py + SH + 2 * B)
            # подпись — в самую большую свободную зону сверху/снизу
            cap_box = None
            if cap and spec.get("caption_pos") != "none":
                top_free, bot_free = max(0, phone[1]), max(0, H - phone[3])
                where = spec.get("caption_pos") or ("bottom" if bot_free >= top_free else "top")
                zone = (0, top_free) if where == "top" else (phone[3], H)
                zh = zone[1] - zone[0]
                if zh < 140:
                    issues.append("Подписи мало места: увеличь zoom-кадр иначе (focus_at) или убери caption.")
                lines = cap.count("\n") + 1
                fs = max(40, min(112, zh * 0.62 / lines, W * 0.86 / max(8, max(len(x) for x in cap.split("\n")) * 0.52)))
                ch = fs * lines
                cy = zone[0] + (zh - ch) / 2
                await pg.evaluate("([t, y, fs]) => { const c = document.getElementById('cap'); c.textContent = t; c.style.top = y + 'px'; c.style.fontSize = fs + 'px' }",
                                  [cap, cy, fs])
                await pg.evaluate("document.fonts.ready")
                # реальные границы текста — чтобы шейпы держались от него подальше, но не занимали всю ширину
                cap_box = tuple(await pg.evaluate("() => { const r = document.createRange(); r.selectNodeContents(document.getElementById('cap'));"
                                                  " const b = r.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom] }"))
            rad = round(min(SW, SH) * 0.11 + B)
            await pg.evaluate("""([x, y, r, B]) => { const p = document.getElementById('phone'); p.style.left = x + 'px'; p.style.top = y + 'px';
                p.style.borderRadius = r + 'px'; p.querySelector('.scr').style.borderRadius = (r - B) + 'px' }""", [px, py, rad, B])
            await pg.evaluate("h => { document.getElementById('bd').innerHTML = h }",
                              backdrop(W, H, pal, spec.get("motifs"), cap_box, phone, f"{name}|{cap}|{pal}"))
            if cap_box:
                over = await pg.evaluate("() => { const c = document.getElementById('cap'); return c.scrollWidth > c.clientWidth + 2 }")
                if over:
                    issues.append("Подпись не влезает по ширине — сократи или разбей на строки (\\n).")

            # 3) замедляем время и снимаем кадры
            cdp = await pg.context.new_cdp_session(pg)
            await cdp.send("Animation.enable")
            t = time.time()
            await pg.locator("#canvas").screenshot(path=str(work / "probe.png"))
            shot = time.time() - t
            rate = int(min(30, max(8, math.ceil(shot / 0.018))))   # шаг ≤ 18 мс итогового ролика → ~50+ fps
            await cdp.send("Animation.setPlaybackRate", {"playbackRate": 1 / rate})

            frames, stop = [], False

            async def shooter():
                i = 0
                while not stop:
                    t1 = time.time()
                    path = work / f"{i:05d}.png"
                    await pg.locator("#canvas").screenshot(path=str(path))
                    frames.append((t1, path))
                    i += 1

            async def wait(sec):
                await asyncio.sleep(float(sec) * rate)

            async def tap(sel):
                loc = fr.locator(sel).first
                if not await loc.count():
                    issues.append(f"tap: не нашёл {sel}")
                    return
                pt = await loc.evaluate("e => { const r = e.getBoundingClientRect(); return [r.x + r.width/2, r.y + r.height/2] }")
                await fr.evaluate("([x,y]) => { const t=document.createElement('div'); t.className='dsg-tap'; t.style.left=x+'px';"
                                  " t.style.top=y+'px'; document.body.append(t); setTimeout(()=>t.remove(), 30000) }", pt)
                try:
                    await loc.click(force=True, timeout=5000)
                except Exception as e:
                    issues.append(f"tap {sel}: {str(e).splitlines()[0][:120]}")

            async def scroll(sel, dur):
                y = await fr.evaluate("s => { const e = document.querySelector(s); if (!e) return null;"
                                      " const r = e.getBoundingClientRect(); return r.top + scrollY - innerHeight * 0.45 }", sel)
                if y is None:
                    issues.append(f"scroll_to: не нашёл {sel}")
                    return
                y0 = await fr.evaluate("scrollY")
                n = max(2, int(float(dur) * 50))
                for k in range(n + 1):
                    e = k / n
                    e = e * e * (3 - 2 * e)
                    await fr.evaluate(f"scrollTo(0, {y0 + (max(0, y) - y0) * e})")
                    await asyncio.sleep(float(dur) * rate / n)

            task = asyncio.create_task(shooter())
            t0 = time.time()
            await wait(spec.get("lead", 0.4))
            steps = (spec.get("steps") or [])[:60]
            for i, st in enumerate(steps):
                if progress and i % 4 == 0:
                    await progress(f"ролик: шаг {i + 1}/{len(steps)}")
                if "tap" in st:
                    await tap(st["tap"])
                elif "wait" in st:
                    await wait(min(4, float(st["wait"])))
                elif "js" in st:
                    try:
                        await fr.evaluate(f"() => {{ {st['js']} }}")
                    except Exception as e:
                        issues.append(f"js: {str(e).splitlines()[0][:120]}")
                elif "scroll_to" in st:
                    await scroll(st["scroll_to"], st.get("dur", 0.8))
                if (time.time() - t0) / rate > 20:
                    issues.append("Ролик длиннее 20 с — обрезал. Сократи сценарий.")
                    break
            await wait(spec.get("tail", 0.3))
            stop = True
            await task
        finally:
            await b.close()

    frames = [((t - t0) / rate, p) for t, p in frames if (t - t0) / rate >= 0.1]
    if len(frames) < 10:
        return {"issues": issues + ["Слишком мало кадров — сценарий пустой?"]}
    # цикл: первый и последний кадр должны совпасть
    if spec.get("loop", True):
        from PIL import Image, ImageChops
        d = ImageChops.difference(Image.open(frames[0][1]).convert("RGB"), Image.open(frames[-1][1]).convert("RGB"))
        if d.point(lambda v: 255 if v > 14 else 0).getbbox():
            issues.append("Цикл не сходится: последний кадр отличается от первого — верни интерфейс в исходное состояние "
                          "в конце сценария (например, сними реакцию), иначе при повторе будет скачок.")
    lst = work / "list.txt"
    rows = []
    for (t, pth), (t2, _) in zip(frames, frames[1:] + [(frames[-1][0] + 0.02, None)]):
        rows.append(f"file '{pth}'\nduration {t2 - t:.4f}")
    rows.append(f"file '{frames[-1][1]}'")
    lst.write_text("\n".join(rows), "utf-8")
    mp4, gif = out_dir / f"{name}.mp4", out_dir / f"{name}.gif"
    gw = 720 if W <= 1080 else 960

    def enc():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-vf",
                        f"fps=50,scale={W}:{H}:flags=lanczos:out_color_matrix=bt709:out_range=tv,format=yuv420p",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-colorspace", "bt709", "-color_primaries", "bt709",
                        "-color_trc", "bt709", "-color_range", "tv", "-movflags", "+faststart", str(mp4)], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-vf",
                        f"fps=30,scale={gw}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];"
                        f"[b][p]paletteuse=dither=sierra2_4a", "-loop", "0", str(gif)], check=True)
        # лист превью для агента: 6 моментов ролика
        from PIL import Image
        dur = frames[-1][0]
        picks = [min(frames, key=lambda f: abs(f[0] - dur * k / 5))[1] for k in range(6)]
        tw = 360
        th = round(tw * H / W)
        sheet = Image.new("RGB", (tw * 3, th * 2), "white")
        for k, pth in enumerate(picks):
            sheet.paste(Image.open(pth).convert("RGB").resize((tw, th)), ((k % 3) * tw, (k // 3) * th))
        sp = out_dir / f".{name}-sheet.jpg"
        sheet.save(sp, quality=88)
        return sp

    sheet = await asyncio.to_thread(enc)
    shutil.rmtree(work, ignore_errors=True)
    return {"mp4": mp4, "gif": gif, "sheet": sheet, "issues": issues, "seconds": round(frames[-1][0], 1),
            "fps_capture": round(len(frames) / max(0.1, frames[-1][0]))}


async def inspect(source: str, css: str = "", js: str = "") -> tuple[str, Path]:
    """Карта страницы для сценария: кнопки, ссылки, элементы с id/классами/aria — с координатами (мобильная ширина 375)."""
    from playwright.async_api import async_playwright
    src = _resolve(source)
    shot = config.DATA / "tmp" / f"inspect-{hashlib.md5(src.encode()).hexdigest()[:8]}.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--allow-file-access-from-files"])
        try:
            pg = await b.new_page(viewport={"width": 375, "height": 812})
            await pg.goto(src, wait_until="load", timeout=45000)
            await pg.wait_for_timeout(1200)
            if css:
                await pg.add_style_tag(content=css)
            if js:
                await pg.evaluate(f"() => {{ {js} }}")
            outline = await pg.evaluate("""() => {
              const out = []; const seen = new Set();
              const sel = e => { let s = e.tagName.toLowerCase(); if (e.id) s += '#' + e.id;
                const c = [...e.classList].slice(0, 3); if (c.length) s += '.' + c.join('.');
                for (const a of e.getAttributeNames()) if (a.startsWith('data-')) s += `[${a}="${e.getAttribute(a)}"]`; return s };
              const els = document.querySelectorAll('button,a,input,[role],[aria-label],[id],section,header,nav,h1,h2,h3,[class*=island],[class*=nav],[class*=reaction],[class*=pill],[data-id]');
              for (const e of els) {
                const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
                if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                const key = sel(e); if (seen.has(key) && !e.dataset.id) continue; seen.add(key);
                const t = (e.getAttribute('aria-label') || e.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
                out.push(`${key} | ${Math.round(r.x)},${Math.round(r.y + scrollY)} ${Math.round(r.width)}x${Math.round(r.height)}${cs.position === 'fixed' ? ' fixed' : ''} | ${t}`);
                if (out.length > 160) break;
              }
              return `Высота страницы: ${document.documentElement.scrollHeight}px\\n` + out.join('\\n');
            }""")
            await pg.screenshot(path=str(shot))
        finally:
            await b.close()
    return outline, shot
