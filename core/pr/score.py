"""Три слоя оценки статьи (ТЗ pr-agent.md, раздел 3).

1. filter_reason() — жёсткий фильтр без ИИ: блоги компаний, переводы, короткие, реклама, не про дизайн.
2. signal()        — сигналы площадки на сутки возраста статьи, 0–10. Новичка не выкидывает, только понижает.
3. ai_rate()       — рубрика Claude по манифесту дизайнерс, 0–10, + фраза «почему» для карточки.
Итог: total = 0.7 × Claude + 0.3 × площадка.
"""
import logging
import math
import re
import time

import anthropic

from core import config, spend
from core.pr import store
from core.pr.sources import Article

log = logging.getLogger("pr.score")

MODEL = config.env("PR_MODEL", "") or config.SEO_CLASSIFY_MODEL or config.MODEL
MIN_CHARS = int(config.env("PR_MIN_CHARS", "5000"))
MIN_IMAGES = int(config.env("PR_MIN_IMAGES", "5"))
W_AI = float(config.env("PR_WEIGHT_AI", "0.6"))
W_AUTHOR = float(config.env("PR_WEIGHT_AUTHOR", "0.2"))      # остальное — сигналы площадки
MIN_DESIGN_DENSITY = float(config.env("PR_MIN_DESIGN_DENSITY", "8"))   # дизайн-слов на 10 000 знаков

DESIGN_WORDS = re.compile(
    r"(?i)дизайн|\bux\b|\bui\b|интерфейс|юзабилити|usability|figma|фигм|типограф|шрифт|айдентик|брендинг|логотип|"
    r"иллюстрац|моушн|motion|анимаци|прототип|вайрфрейм|макет|дизайнер|арт-дирек|визуал|верстк|лендинг|портфолио")
AD_WORDS = re.compile(
    r"(?i)промокод|скидк[аиу] на (курс|обучение)|запишитесь на (курс|консультацию|бесплатн)|оставьте заявку|"
    r"переходите по ссылке, чтобы купить|старт потока|набор на курс|наш курс")


def filter_reason(a: Article, kind: str) -> str:
    """Почему статья отсеяна на слое 1, или '' — прошла."""
    if a.translation:
        return "перевод"
    if len(a.text) < MIN_CHARS and a.images < MIN_IMAGES:
        return "короткая"
    if not a.corporate and len(AD_WORDS.findall(a.text)) >= 2:
        return "реклама"        # у компаний продающие абзацы — норма, их не отсеиваем
    if kind != "habr" and not DESIGN_WORDS.search(f"{a.title}\n{a.text[:3000]}"):
        return "не про дизайн"       # общие ленты (vc) — в начале текста должно быть про дизайн
    # в хабы дизайна часто попадают статьи разработчиков: проверяем, сколько текста про дизайн
    hits = len(DESIGN_WORDS.findall(f"{a.title}\n{a.text}"))
    per10k = hits * 10000 / max(len(a.text), 1)
    if per10k < MIN_DESIGN_DENSITY and not DESIGN_WORDS.search(a.title):
        return "мало про дизайн"
    return ""


def _lg(x: float) -> float:
    return math.log2(1 + max(0.0, x))


def signal(a: Article) -> float:
    """Сигналы площадки, нормированные по возрасту (минимум сутки), 0–10."""
    days = max(1.0, (time.time() - (a.published or time.time())) / 86400)
    st = a.stats or {}
    # ~10 баллов: 50 закладок, 20 комментариев, рейтинг +30 и 10 тыс. просмотров в сутки
    s = (0.7 * _lg(st.get("favorites", 0) / days)
         + 0.5 * _lg(st.get("comments", 0) / days)
         + 0.4 * _lg(st.get("score", 0) / days)
         + 0.3 * _lg(st.get("views", 0) / days / 300))
    au = a.author_stats or {}
    if au.get("posts", 0) >= 5 or au.get("karma", 0) >= 20 or au.get("followers", 0) >= 300:
        s += 0.7
    return round(min(10.0, s), 1)


SYSTEM = """Ты — редактор платформы дизайнерс (dsgners.ru). Ищешь на других площадках авторов, которых стоит позвать публиковаться у нас.

Манифест дизайнерс: мы за полифонию и авторство. Место, где можно поделиться статьёй, которую сам бы читал, кейсом, которым гордишься, факапами, которые чему-то научили, или просто тем, что волнует как дизайнера и как человека. Мы не учебник: живой дизайн рядом с рефлексией, за каждым текстом видно человека.

Оцени статью по рубрике, каждый пункт 0–2:
1. personal — личный опыт: свой кейс, свой проект, свои факапы, а не пересказ чужого.
2. position — своя позиция: автор спорит, делает выводы, а не собирает «10 советов».
3. specifics — конкретика: цифры, процесс, скрины, до/после.
4. useful — польза дизайнеру: коллега узнает новое или задумается.
5. no_ads — без рекламы: текст не ведёт к продаже курса, услуги, сервиса (2 — рекламы нет). Для блога компании оценивай иначе: 2 — полезный материал, где продукт или студия — фон; 0 — текст целиком продающий.

Ещё определи:
- author_kind: person — пишет конкретный человек от себя (дизайнер студии от своего имени — тоже person); company — блог компании, студии, сервиса; school — онлайн-школа или курсы;
- design_focus — насколько статья про дизайн:
  2 — дизайн главная тема: работа дизайнера, UX/UI, продуктовый дизайн, исследования пользователей, бренд и айдентика, графика, иллюстрация, типографика, моушн, карьера и процессы дизайнера. Автор смотрит на задачу как дизайнер;
  1 — дизайн побочный: статья про разработку, автоматизацию, ИИ-ботов, код, геймдев, аналитику, менеджмент — дизайн упомянут или есть раздел про интерфейс, но читать её будут не дизайнеры;
  0 — не про дизайн.
  Будь строгим: «разработчик сделал бота / автоматизировал отчёт / отладил игру» — это 1, даже если там есть шрифты или анимации.
- design: true только если design_focus = 2;
- type: кейс | мнение | гайд | новость | интервью | другое;
- why: одна фраза до 140 знаков для владельца — чем статья сильная или слабая, конкретно про эту статью, без общих слов.

Блоги компаний нам тоже интересны (особенно студии, агентства и продуктовые компании с дизайн-кейсами) — оценивай их по той же рубрике, author_kind укажи честно.

Оценивай строго: 8–10 — редкие тексты, которые хочется переслать коллеге. Гайды-пересказы и SEO-тексты — низко."""

TOOL = {
    "name": "rate_article",
    "description": "Оценка статьи по рубрике дизайнерс",
    "input_schema": {
        "type": "object",
        "properties": {
            "personal": {"type": "integer", "minimum": 0, "maximum": 2},
            "position": {"type": "integer", "minimum": 0, "maximum": 2},
            "specifics": {"type": "integer", "minimum": 0, "maximum": 2},
            "useful": {"type": "integer", "minimum": 0, "maximum": 2},
            "no_ads": {"type": "integer", "minimum": 0, "maximum": 2},
            "author_kind": {"type": "string", "enum": ["person", "company", "school"]},
            "design_focus": {"type": "integer", "minimum": 0, "maximum": 2},
            "design": {"type": "boolean"},
            "type": {"type": "string", "enum": ["кейс", "мнение", "гайд", "новость", "интервью", "другое"]},
            "why": {"type": "string"},
        },
        "required": ["personal", "position", "specifics", "useful", "no_ads", "author_kind", "design_focus", "design", "type", "why"],
    },
}


def calibration() -> str:
    """Отметки владельца 👍/👎 — примеры для рубрики."""
    up, down = store.voted(1), store.voted(-1)
    if not up and not down:
        return ""
    def line(r):
        return f"- «{r['title']}» ({r['site']}) — оценка была {r.get('ai') or '?'}: {(r.get('ai_json') or {}).get('why', '')}"
    parts = ["Как владелец оценил прошлые статьи — подстрой оценки под его вкус:"]
    if up:
        parts += ["Понравились (👍):"] + [line(r) for r in up]
    if down:
        parts += ["Не понравились (👎) — такие оценивай ниже:"] + [line(r) for r in down]
    return "\n".join(parts)


def over_budget() -> bool:
    return spend.this_month() >= config.MONTHLY_LIMIT_USD


async def ai_rate(a: Article) -> dict:
    """Рубрика Claude. Возвращает dict из TOOL + score (0–10) + cost."""
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    system = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
    cal = calibration()
    if cal:
        system.append({"type": "text", "text": cal})
    prompt = (f"Площадка: {a.site}\nАдрес: {a.url}\nЗаголовок: {a.title}\nАвтор: {a.author}\n"
              f"Хабы/разделы: {', '.join(a.hubs) or '—'}\nКартинок: {a.images}; знаков: {len(a.text)}\n\n"
              f"Текст (может быть обрезан):\n{a.text[:14000]}")
    resp = await client.messages.create(model=MODEL, max_tokens=600, system=system, tools=[TOOL],
                                        tool_choice={"type": "tool", "name": "rate_article"},
                                        messages=[{"role": "user", "content": prompt}])
    cost = spend.record("pr", resp.usage, MODEL)
    out = next((b.input for b in resp.content if b.type == "tool_use"), None) or {}
    out["score"] = sum(int(out.get(k, 0)) for k in ("personal", "position", "specifics", "useful", "no_ads"))
    out["cost"] = round(cost, 4)
    return out


def author_score(stats: dict, history: tuple[int, int] = (0, 0)) -> float | None:
    """Вес автора по профилю, 0–10. None — площадка ничего не отдала.
    ~8: 30 публикаций, карма 60, 2 тыс. подписчиков; ~5: 5 публикаций и пара сотен подписчиков;
    плюс до 2 баллов за его сильные статьи, которые мы уже видели.
    history — (статей автора у нас в базе, из них с итогом ≥ порога)."""
    st = stats or {}
    posts, karma, rating, foll = (st.get(k, 0) or 0 for k in ("posts", "karma", "rating", "followers"))
    if not any((posts, karma, rating, foll)):
        return None
    s = (1.4 * _lg(posts)                 # 31 публикация → 7
         + 0.7 * _lg(karma)               # карма 63 → 4.2
         + 0.3 * _lg(rating)
         + 0.7 * _lg(foll / 10))          # 2 тыс. подписчиков → 5.4
    s = s * 0.5 + min(2.0, history[1] * 1.0)
    return round(min(10.0, s), 1)


def total(ai: float, sig: float, auth: float | None = None) -> float:
    if auth is None:
        return round(W_AI / (W_AI + (1 - W_AI - W_AUTHOR)) * ai + (1 - W_AI - W_AUTHOR) / (W_AI + (1 - W_AI - W_AUTHOR)) * sig, 1)
    return round(W_AI * ai + W_AUTHOR * auth + (1 - W_AI - W_AUTHOR) * sig, 1)
