"""Сценарий поста SMM в два шага.

1. SMM-щик пишет 3 варианта текста (строгий / обычный / свойский). Владелец берёт вариант, правит его своим текстом
   или просит SMM-щика переписать конкретный вариант.
2. По выбранному тексту дизайнер делает 3 картинки. Владелец выбирает одну → готовый пост (картинка + текст)
   → «Опубликовать» (если канал известен) или забрать черновиком.

Состояние — в data/smm_flow.json (переживает перезапуск бота).
"""
import json
import time
import uuid
from pathlib import Path

from core import config

STYLES = {"strict": "Строгий", "normal": "Обычный", "bar": "Свойский"}
STYLE_ICON = {"strict": "1️⃣", "normal": "2️⃣", "bar": "3️⃣"}
_F = config.DATA / "smm_flow.json"


def _load() -> dict:
    try:
        return json.loads(_F.read_text("utf-8"))
    except Exception:
        return {}


def _save(d: dict):
    # держим только последние 200 сценариев
    if len(d) > 200:
        for k in sorted(d, key=lambda k: d[k].get("created", 0))[:-200]:
            d.pop(k)
    _F.write_text(json.dumps(d, ensure_ascii=False, indent=1), "utf-8")


def new(**fields) -> str:
    d = _load()
    fid = uuid.uuid4().hex[:8]
    d[fid] = {"created": time.time(), **fields}
    _save(d)
    return fid


def get(fid: str) -> dict | None:
    return _load().get(fid)


def update(fid: str, **fields) -> dict:
    d = _load()
    d.setdefault(fid, {"created": time.time()}).update(fields)
    _save(d)
    return d[fid]


def by_designer_job(jid: str) -> tuple[str, dict] | None:
    for k, v in _load().items():
        if v.get("designer_job") == jid:
            return k, v
    return None


def designer_task(flow: dict) -> str:
    shots = "\n".join(f"- {s}" for s in flow.get("shots", [])[:8])
    return (
        "Сделай 3 РАЗНЫХ варианта картинки к посту в Telegram — владелец выберет один. Варианты должны заметно "
        "отличаться: разные шаблоны или композиции и палитры (например promo, promo-duo, showcase / карточка). "
        "Три файла — три вызова render_visual с разными name (var-1, var-2, var-3), в finish отдай все три.\n\n"
        f"Заголовок для картинки (если нужен): {flow.get('image_title') or 'придумай коротко по тексту'}\n"
        f"Формат: {flow.get('format') or 'tg-post'}\n\n"
        f"Текст поста:\n{flow.get('text', '')}\n"
        + (f"\nЭкраны из Figma, уже выгруженные SMM-щиком (бери их):\n{shots}\n" if shots else
           "\nЕсли пост про фичу сайта — найди экраны в Figma сам.\n")
    )


def by_msg(msg_id: int) -> tuple[str, str] | None:
    """Сообщение с вариантом текста → (flow_id, стиль)."""
    for k, v in _load().items():
        st = (v.get("msgs") or {}).get(str(msg_id))
        if st:
            return k, st
    return None


def by_rework(jid: str) -> dict | None:
    for v in _load().values():
        if jid in (v.get("rework_jobs") or []):
            return v
    return None
