"""Публикация статьи редакции на сайт после «Ок» владельца: картинки → текст → SEO-поля.

Берёт из задачи SEO-шника: article.md (текст), картинки от дизайнера, meta.article (заголовок, обложка, теги,
адрес — если это обновление) и meta.seo_fields. Статьи авторов не публикует и не меняет.
"""
import re
from pathlib import Path

from core import config, site_api

MARKERS = re.compile(r"\[(РАБОТА|ПРОВЕРИТЬ)[^\]]*\]")


def md_to_html(md: str) -> tuple[str, str]:
    """Markdown → (заголовок H1, HTML тела без H1)."""
    import markdown
    m = re.match(r"\s*#\s+(.+)\n", md)
    title = m.group(1).strip() if m else ""
    body = md[m.end():] if m else md
    html = markdown.markdown(body, extensions=["tables", "sane_lists"])
    return title, html


def _files(job: dict) -> dict[str, Path]:
    out = {}
    for f in job.get("files", []):
        p = config.OUTBOX / f
        if p.exists():
            out.setdefault(p.name, p)
    return out


def problems(job: dict) -> list[str]:
    """Почему статью нельзя публиковать автоматически (пусто — можно)."""
    files = _files(job)
    md = files.get("article.md")
    if not md:
        return ["нет article.md"]
    found = sorted(set(m.group(0)[:60] for m in MARKERS.finditer(md.read_text("utf-8"))))
    if found:
        return ["в тексте остались пометки для редактора: " + "; ".join(found[:6])]
    return []


async def publish(job: dict, own_check) -> tuple[str, list[str]]:
    """Вернёт (адрес статьи, отчёт по шагам). own_check(url) — статья редакции?"""
    meta = job.get("meta") or {}
    art = meta.get("article") or {}
    files = _files(job)
    md = files["article.md"].read_text("utf-8")
    h1, html = md_to_html(md)
    title = art.get("title") or h1
    log = []

    # картинки в тексте: ![alt](file.png) → загружаем и подставляем адрес на сайте
    uploaded: dict[str, str] = {}
    for name in re.findall(r'<img[^>]+src="([^"]+)"', html):
        base = Path(name).name
        if base in files and base not in uploaded:
            uploaded[base] = await site_api.upload_image(files[base])
    for base, url in uploaded.items():
        html = re.sub(rf'src="[^"]*{re.escape(base)}"', f'src="{url}"', html)
    cover_url = ""
    cover = art.get("cover_file")
    if cover:
        cover = Path(cover).name if Path(cover).suffix else Path(cover).name + ".png"
        if cover in files:
            cover_url = await site_api.upload_image(files[cover])
    log.append(f"картинок загружено: {len(uploaded) + (1 if cover_url else 0)}")

    payload = {"title": title, "html": html}
    if cover_url:
        payload["cover_url"] = cover_url
    if art.get("tags"):
        payload["tags"] = art["tags"]
    if art.get("update_url"):
        if not own_check(art["update_url"]):
            raise site_api.SiteAPIError("это статья автора — текст не меняем")
        cur = await site_api.get_article(art["update_url"])
        aid, url = cur["id"], art["update_url"]
        await site_api.update_article(aid, payload)
        log.append("✅ текст статьи обновлён")
    else:
        payload["author"] = sorted(config.SEO_OWN_AUTHORS)[0]
        res = await site_api.create_article(payload)
        aid, url = res["id"], res["url"]
        log.append("✅ статья опубликована")

    f = dict(meta.get("seo_fields") or {})
    if f:
        f.pop("url", None)
        if f.get("jsonld"):
            f["jsonld"] = f["jsonld"].replace("{URL}", url).replace("{IMAGE}", cover_url)
        if f.get("images"):
            f["images"] = [dict(i, src=uploaded.get(Path(i["src"]).name, i["src"])) for i in f["images"]]
        await site_api.update_seo(aid, f)
        log.append("✅ SEO-поля записаны")
    return url, log
