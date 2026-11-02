"""Навыки агентов — в формате SKILL.md, как у Claude.

Навык = папка с SKILL.md (вверху frontmatter: name, description) и, по желанию, доп. файлами
(примеры, шаблоны, справка — .md/.txt/.html/.css/.json).
Агент всегда видит только список навыков (name + description). Полный текст подгружает
инструментом use_skill, когда задача подходит — поэтому навыков может быть много без лишних токенов.

Где лежат:
  agents/<агент>/skills/<slug>/   — из репозитория (их можно выключить или переопределить в офисе)
  data/skills/<агент>/<slug>/     — созданные/загруженные в офисе (главнее репозитория)
  data/skills/<агент>/_disabled.json — выключенные
"""
import io
import json
import re
import shutil
import zipfile
from pathlib import Path

from core import config

TEXT_EXT = {".md", ".txt", ".html", ".css", ".json", ".csv", ".svg"}
MAX_SKILL_CHARS = 40_000
MAX_ZIP_BYTES = 5 * 1024 * 1024


def _repo_dir(agent: str) -> Path:
    return config.AGENTS / agent / "skills"


def _office_dir(agent: str) -> Path:
    return config.DATA / "skills" / agent


def _disabled_file(agent: str) -> Path:
    return _office_dir(agent) / "_disabled.json"


def slugify(s: str) -> str:
    tr = str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                       "abvgdeezziiklmnoprstufhccss_y_eua")
    s = s.lower().translate(tr)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:50] or "skill"


def parse(text: str) -> tuple[dict, str]:
    """frontmatter → (meta, тело)."""
    meta: dict = {}
    body = text
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip("\"'")
        body = text[m.end():]
    return meta, body


def _disabled(agent: str) -> set:
    f = _disabled_file(agent)
    try:
        return set(json.loads(f.read_text("utf-8")))
    except Exception:
        return set()


def _folders(agent: str) -> dict[str, tuple[Path, str]]:
    """slug -> (папка, источник). Офис перекрывает репозиторий."""
    res: dict = {}
    for base, src in ((_repo_dir(agent), "repo"), (_office_dir(agent), "office")):
        if base.exists():
            for d in sorted(base.iterdir()):
                if d.is_dir() and (d / "SKILL.md").exists():
                    res[d.name] = (d, src)
    return res


def list_skills(agent: str, enabled_only: bool = False) -> list[dict]:
    off = _disabled(agent)
    out = []
    for slug, (d, src) in _folders(agent).items():
        meta, body = parse((d / "SKILL.md").read_text("utf-8", errors="ignore"))
        item = {
            "slug": slug,
            "name": meta.get("name") or slug,
            "description": meta.get("description", ""),
            "source": src,
            "enabled": slug not in off,
            "files": sorted(p.name for p in d.iterdir() if p.is_file() and p.name != "SKILL.md"),
            "has_repo": (_repo_dir(agent) / slug / "SKILL.md").exists(),
        }
        if not enabled_only or item["enabled"]:
            out.append(item)
    return out


def system_section(agent: str) -> str:
    items = list_skills(agent, enabled_only=True)
    if not items:
        return ""
    lines = [f"- `{s['slug']}` — {s['name']}: {s['description'] or 'без описания'}" for s in items]
    return ("# Твои навыки\n"
            "Прежде чем работать, проверь список. Если задача подходит под навык — сначала вызови "
            "use_skill с его кодом и строго следуй инструкции навыка (она главнее общего порядка работы). "
            "Можно взять несколько навыков.\n" + "\n".join(lines))


def skill_text(agent: str, slug: str) -> str:
    f = _folders(agent).get(slug)
    if not f or slug in _disabled(agent):
        names = ", ".join(s["slug"] for s in list_skills(agent, True)) or "нет"
        raise ValueError(f"Нет такого навыка. Доступные: {names}")
    d = f[0]
    _, body = parse((d / "SKILL.md").read_text("utf-8", errors="ignore"))
    parts = [body.strip()]
    for p in sorted(d.iterdir()):
        if p.is_file() and p.name != "SKILL.md" and p.suffix.lower() in TEXT_EXT:
            parts.append(f"\n## Файл навыка: {p.name}\n```\n{p.read_text('utf-8', errors='ignore')}\n```")
    text = "\n".join(parts)
    return text[:MAX_SKILL_CHARS] + ("\n[…обрезано]" if len(text) > MAX_SKILL_CHARS else "")


def read_raw(agent: str, slug: str) -> str:
    f = _folders(agent).get(slug)
    if not f:
        raise ValueError("Нет такого навыка")
    return (f[0] / "SKILL.md").read_text("utf-8", errors="ignore")


def save(agent: str, slug: str | None, name: str, description: str, body: str) -> str:
    """Создать или изменить навык (сохраняется в офисной папке)."""
    name = name.strip() or "Навык"
    slug = slug or slugify(name)
    d = _office_dir(agent) / slug
    src = _folders(agent).get(slug)
    if src and src[1] == "repo" and not d.exists():  # переопределяем репозиторный — копируем доп. файлы
        shutil.copytree(src[0], d)
    d.mkdir(parents=True, exist_ok=True)
    desc = " ".join(description.split())
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body.strip()}\n", "utf-8")
    return slug


def set_enabled(agent: str, slug: str, enabled: bool):
    off = _disabled(agent)
    (off.discard if enabled else off.add)(slug)
    f = _disabled_file(agent)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(sorted(off), ensure_ascii=False), "utf-8")


def delete(agent: str, slug: str):
    """Удаляет офисную версию. Навык из репозитория удалить нельзя — только выключить."""
    d = _office_dir(agent) / slug
    if d.exists():
        shutil.rmtree(d)
    elif (_repo_dir(agent) / slug).exists():
        raise ValueError("Это навык из репозитория — его можно только выключить")


def upload(agent: str, filename: str, data: bytes) -> str:
    """SKILL.md (или любой .md с frontmatter) либо .zip с папкой навыка."""
    low = filename.lower()
    if low.endswith(".md"):
        text = data.decode("utf-8", errors="ignore")
        meta, body = parse(text)
        name = meta.get("name") or Path(filename).stem
        return save(agent, slugify(meta.get("name") or Path(filename).stem), name, meta.get("description", ""), body)
    if low.endswith(".zip"):
        if len(data) > MAX_ZIP_BYTES:
            raise ValueError("Архив больше 5 МБ")
        zf = zipfile.ZipFile(io.BytesIO(data))
        skill_md = [n for n in zf.namelist() if n.rsplit("/", 1)[-1] == "SKILL.md" and "__MACOSX" not in n]
        if not skill_md:
            raise ValueError("В архиве нет SKILL.md")
        root = skill_md[0].rsplit("/", 1)[0] + "/" if "/" in skill_md[0] else ""
        meta, _ = parse(zf.read(skill_md[0]).decode("utf-8", errors="ignore"))
        slug = slugify(meta.get("name") or (root.strip("/").rsplit("/", 1)[-1] if root else Path(filename).stem))
        d = _office_dir(agent) / slug
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        for n in zf.namelist():
            if not n.startswith(root) or n.endswith("/") or "__MACOSX" in n:
                continue
            rel = n[len(root):]
            if "/" in rel or Path(rel).suffix.lower() not in TEXT_EXT | {".md"}:
                continue  # берём только текстовые файлы верхнего уровня папки навыка
            (d / rel).write_bytes(zf.read(n)[:MAX_SKILL_CHARS * 4])
        return slug
    raise ValueError("Загружай SKILL.md или .zip с папкой навыка")


# --- инструмент для агентов ---
USE_SKILL_TOOL = {
    "name": "use_skill",
    "description": "Загрузить полную инструкцию навыка по его коду из списка «Твои навыки».",
    "input_schema": {"type": "object", "properties": {"skill": {"type": "string"}}, "required": ["skill"]},
}
