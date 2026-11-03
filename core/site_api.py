"""Клиент API сайта dsgners.ru для агентов (ТЗ для разработчика — docs/site-api.md).

Что умеет (только после «Ок» владельца):
  get_article(url)            — данные статьи: id, автор, текущие SEO-поля, картинки
  update_seo(id, fields)      — записать SEO-поля (title, description, OG, alt, разметка); текст статьи не трогает
  message_author(id, ...)     — письмо автору от редакции: email + уведомление на сайте со ссылкой на письмо

Пока в .env нет SITE_API_URL и SITE_API_TOKEN — enabled() = False, и агенты присылают пакеты владельцу, как раньше.
"""
import aiohttp

from core import config


class SiteAPIError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.SITE_API_URL and config.SITE_API_TOKEN)


async def _call(method: str, path: str, params=None, payload=None) -> dict:
    url = config.SITE_API_URL.rstrip("/") + path
    headers = {"Authorization": f"Bearer {config.SITE_API_TOKEN}", "User-Agent": "dsgners-agents/1.0"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as s:
        async with s.request(method, url, params=params, json=payload, headers=headers) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {"error": (await r.text())[:300]}
            if r.status >= 400:
                raise SiteAPIError(f"{method} {path}: HTTP {r.status} {str(data)[:300]}")
            return data or {}


async def get_article(url: str) -> dict:
    return await _call("GET", "/article", params={"url": url})


SEO_KEYS = ("seo_title", "meta_description", "og_title", "og_description", "jsonld", "images")


async def update_seo(article_id, fields: dict) -> dict:
    payload = {k: v for k, v in fields.items() if k in SEO_KEYS and v not in (None, "", [], {})}
    if not payload:
        raise SiteAPIError("нечего записывать — SEO-поля пустые")
    return await _call("PATCH", f"/article/{article_id}/seo", payload=payload)


async def message_author(article_id, subject: str, text: str) -> dict:
    return await _call("POST", "/author-message", payload={"article_id": article_id, "subject": subject, "text": text})
