"""Тексты сообщений авторам (ТЗ, раздел 6). Каждое — личное и не похоже на предыдущие.

Обязательно в каждом:
1. по имени (если имени нет — без имени, не выдумываем) + смайл приветствия;
2. почему пишем: увидели его статью «…» на площадке + одна конкретная деталь, по которой видно, что прочитали;
3. зовём на дизайнерс и говорим, чем он хорош (PR_PITCH — только эти факты, цифр сверх них не придумываем);
4. легко отказаться.
Уникальность: смайл, порядок доводов и заход выбираются случайно; последние сообщения идут в промпт как «не повторяй»,
слишком похожий текст переписывается. Правки владельца («✏️») копятся в правила.
"""
import difflib
import logging
import random
import re

import anthropic

from core import config, spend
from core.pr import store

log = logging.getLogger("pr.outreach")

MODEL = config.env("PR_MSG_MODEL", "") or config.MODEL
SENDER = config.env("PR_SENDER_NAME", "") or "менеджер дизайнерс"
PITCH = [x.strip() for x in (config.env("PR_PITCH", "") or
         "похожие статьи у нас собирают больше охватов|"
         "узкая аудитория — только дизайнеры, без случайных читателей|"
         "мы сами продвигаем статьи в своих медиа — это больше 100 каналов").split("|") if x.strip()]
# Для блогов компаний — свои доводы (через «|»); по умолчанию те же, что для авторов
PITCH_COMPANY = [x.strip() for x in (config.env("PR_PITCH_COMPANY", "")).split("|") if x.strip()] or PITCH
EMOJI = ["👋", "🙌", "✌️", "😊", "🙂", "🤝", "👀", "✨"]
OPENERS = ["сразу с того, что зацепило в статье", "с короткого «наткнулись на вашу статью»",
           "с вопроса-комплимента про конкретный момент статьи", "с того, кто пишет, и сразу к статье",
           "с короткой реакции на тему статьи"]
MAX_LEN = int(config.env("PR_MSG_MAX", "600"))
SITE_NAME = {"habr": "Хабре", "vc": "vc.ru"}

SYSTEM = f"""Ты пишешь первое личное сообщение автору статьи от имени {SENDER} из команды дизайнерс — платформы, где дизайнеры публикуют статьи, кейсы и рефлексию.

Сообщение обязательно:
1. Начинается с приветствия по имени и смайла, который дан в задаче. Имя — только из имени автора; если там ник или непонятно — приветствие без имени. Не выдумывай имя.
2. Говорит, почему пишем: увидели его статью (название в «ёлочках») на площадке, и одна конкретная деталь из статьи, по которой видно, что её прочитали. Не «классная статья», а что именно зацепило.
3. Зовёт опубликовать статью на дизайнерс (кросспост можно, ссылка на оригинал остаётся) и объясняет, чем это полезно автору — только доводами из списка в задаче, своими словами. Других цифр и обещаний не добавляй: ни денег, ни мест в рейтинге, ни сроков.
4. Заканчивается тем, что отказаться легко: если неинтересно — просто ответить, больше не потревожим.

Тон — на равных, как дизайнер коллеге в чате. «ты» или «вы» — по тону самой статьи; по умолчанию «вы». Без канцелярита («уважаемый», «уникальная возможность», «в рамках»), без ИИ-штампов, без восклицательных каскадов. Название платформы — «дизайнерс», строчными, не склоняем («на дизайнерс»). Один смайл в приветствии, максимум ещё один в конце. Без ссылок — их добавят отдельно, если нужно. До {MAX_LEN} знаков, 3–5 коротких абзацев.

Каждое сообщение должно звучать по-своему: другие слова, другой заход, другой порядок доводов. Никаких шаблонных фраз, которые повторяются из сообщения в сообщение."""

TOOL = {"name": "message", "description": "Готовое сообщение автору",
        "input_schema": {"type": "object", "properties": {
            "first_name": {"type": "string", "description": "имя для приветствия или пусто"},
            "address": {"type": "string", "enum": ["ты", "вы"]},
            "text": {"type": "string"}}, "required": ["first_name", "address", "text"]}}


def rules() -> list[str]:
    return store.get("msg_rules", [])


def add_rule(text: str):
    r = rules()
    r.append(text.strip()[:300])
    store.put("msg_rules", r[-30:])


def _too_similar(text: str, recent: list[str]) -> float:
    return max((difflib.SequenceMatcher(None, text.lower(), t.lower()).ratio() for t in recent), default=0.0)


def _bad_numbers(text: str, allowed: str) -> list[str]:
    nums = set(re.findall(r"\d+", text)) - set(re.findall(r"\d+", allowed))
    return sorted(nums)


async def draft(a: dict, match: str, comment: str = "", previous: str = "") -> dict:
    """a — статья из pr.db. match: none (новый автор) | sure/maybe (уже есть профиль на дизайнерс)."""
    recent = store.recent_texts(15)
    company = a.get("track") == "company"
    pitch = (PITCH_COMPANY if company else PITCH)[:]
    random.shuffle(pitch)
    emoji = random.choice(EMOJI)
    opener = random.choice(OPENERS)
    ai = a.get("ai_json") or {}
    task = [f"Автор: {a.get('author') or 'неизвестно'} (профиль: {a.get('author_url') or '—'})",
            f"Статья: «{a['title']}» на {SITE_NAME.get(a['site'], a['site'])}, {a['url']}",
            f"Чем сильна (оценка редактора): {ai.get('why', '—')}; тип: {ai.get('type', '—')}",
            f"Смайл приветствия: {emoji}",
            f"Заход: {opener}",
            "Доводы, чем полезен дизайнерс (в этом порядке, своими словами):\n" + "\n".join(f"- {p}" for p in pitch)]
    if company:
        task.append("Это блог компании, а не личный автор. Обращайся к тому, кто ведёт блог/PR компании — по имени, "
                    "если статья подписана человеком, иначе нейтрально («Здравствуйте» + смайл). Тон чуть деловее, "
                    "на «вы». Предложи публиковать кейсы и статьи компании на дизайнерс. Никаких условий сотрудничества, "
                    "цен, рекламных форматов и обещаний — их обсудит основатель, если компания ответит.")
    elif match in ("sure", "maybe"):
        task.append("У автора уже есть профиль на дизайнерс — скажи об этом тепло: этот текст отлично зашёл бы и у нас.")
    if a.get("excerpt"):
        task.append(f"Начало статьи (для конкретной детали):\n{a['excerpt'][:3500]}")
    if recent:
        task.append("Недавние сообщения — НЕ повторяй их начало, обороты и структуру:\n"
                    + "\n---\n".join(t[:400] for t in recent[:8]))
    if rules():
        task.append("Правила от владельца (обязательно):\n" + "\n".join(f"- {r}" for r in rules()))
    if previous:
        task.append(f"Прошлый вариант (не повторяй его):\n{previous}")
    if comment:
        task.append(f"Владелец просит поправить: {comment}")

    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    out, cost = {}, 0.0
    for attempt in range(2):
        resp = await client.messages.create(
            model=MODEL, max_tokens=900,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=[TOOL], tool_choice={"type": "tool", "name": "message"},
            messages=[{"role": "user", "content": "\n\n".join(task)}])
        cost += spend.record("pr", resp.usage, MODEL)
        out = next((b.input for b in resp.content if b.type == "tool_use"), None) or {}
        text = (out.get("text") or "").strip()
        problems = []
        if _too_similar(text, recent) > 0.55:
            problems.append("слишком похоже на недавние сообщения — перепиши другими словами и другим заходом")
        bad = _bad_numbers(text, " ".join(pitch) + a["title"] + (a.get("excerpt") or "")[:3500])
        if bad:
            problems.append(f"убери цифры, которых нет в доводах и статье: {', '.join(bad)}")
        if len(text) > MAX_LEN + 150:
            problems.append(f"сократи до {MAX_LEN} знаков")
        if not problems:
            break
        task.append("Исправь: " + "; ".join(problems))
    out["text"] = (out.get("text") or "").strip()
    out["cost"] = round(cost, 4)
    return out


FOLLOW_SYSTEM = f"""Ты пишешь одно короткое напоминание от {SENDER} (команда дизайнерс) автору, который прочитал первое сообщение, но не ответил.
2–3 предложения, до 300 знаков, по имени, без давления и без новых обещаний. Мягко: вдруг потерялось; если неинтересно — всё ок, больше не напишем.
Тон тот же, что в первом сообщении. «дизайнерс» строчными, не склоняем. Не повторяй первое сообщение."""


async def follow_up(first_text: str, a: dict) -> str:
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    resp = await client.messages.create(
        model=MODEL, max_tokens=400, system=FOLLOW_SYSTEM,
        messages=[{"role": "user", "content": f"Статья: «{a['title']}». Первое сообщение:\n{first_text}"}])
    spend.record("pr", resp.usage, MODEL)
    return "".join(b.text for b in resp.content if b.type == "text").strip()
