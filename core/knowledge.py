"""Знания агента, которые правятся из офиса.

Тексты (роль, чек-лист, правила): по умолчанию из репозитория agents/<a>/,
после правки в офисе — версия из data/agents/<a>/ (она главнее, git pull её не трогает).
Документы: data/knowledge/<a>/docs/*.md|.txt (pdf конвертируется в текст при загрузке).
Референсы: data/knowledge/<a>/refs/good|bad/*.jpg|png — агент видит их перед работой.
"""
import io
import re
from pathlib import Path

from PIL import Image

from core import config

TEXTS = {
    "ROLE.md": "Роль и порядок работы",
    "checklist.md": "Чек-лист качества",
    "feedback.md": "Утверждённые правила",
    "live-rules.md": "Правила из правок (кнопка «Переделать»)",
}
DOCS_LIMIT = 30_000  # символов документов в контексте агента
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def _override(agent: str) -> Path:
    return config.DATA / "agents" / agent


def kdir(agent: str) -> Path:
    return config.DATA / "knowledge" / agent


def text_path(agent: str, name: str) -> tuple[Path, str]:
    if name == "live-rules.md":
        return config.DATA / "feedback" / f"{agent}.md", "office"
    o = _override(agent) / name
    if o.exists():
        return o, "office"
    return config.AGENTS / agent / name, "repo"


def load_text(agent: str, name: str) -> str:
    p, _ = text_path(agent, name)
    return p.read_text("utf-8") if p.exists() else ""


def save_text(agent: str, name: str, content: str):
    if name not in TEXTS:
        raise ValueError("Этот файл нельзя править")
    p = config.DATA / "feedback" / f"{agent}.md" if name == "live-rules.md" else _override(agent) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, "utf-8")


def reset_text(agent: str, name: str):
    o = _override(agent) / name
    if o.exists():
        o.unlink()


def docs_text(agent: str) -> str:
    d = kdir(agent) / "docs"
    if not d.exists():
        return ""
    parts, total = [], 0
    for f in sorted(d.iterdir()):
        if f.suffix.lower() not in (".md", ".txt"):
            continue
        t = f.read_text("utf-8", errors="ignore").strip()
        if total + len(t) > DOCS_LIMIT:
            t = t[: max(0, DOCS_LIMIT - total)] + "\n[…обрезано]"
        parts.append(f"## Документ: {f.stem}\n{t}")
        total += len(t)
        if total >= DOCS_LIMIT:
            break
    return "\n\n".join(parts)


def refs(agent: str, kind: str) -> list[Path]:
    d = kdir(agent) / "refs" / kind
    return sorted((p for p in d.iterdir() if p.suffix.lower() in IMG_EXT), key=lambda p: p.stat().st_mtime, reverse=True) if d.exists() else []


def note_for(img: Path) -> str:
    n = img.with_suffix(".txt")
    return n.read_text("utf-8").strip() if n.exists() else ""


def _safe(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    stem = re.sub(r"[^\w\-]+", "-", stem or ext, flags=re.U).strip("-")[:60] or "file"
    return f"{stem}.{ext.lower()}" if dot else stem


def save_upload(agent: str, kind: str, filename: str, data: bytes, note: str = "") -> str:
    name = _safe(filename)
    if kind in ("good", "bad"):
        d = kdir(agent) / "refs" / kind
        d.mkdir(parents=True, exist_ok=True)
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((1600, 1600))
        out = d / (Path(name).stem + ".jpg")
        im.save(out, "JPEG", quality=88)
        if note.strip():
            out.with_suffix(".txt").write_text(note.strip(), "utf-8")
        return str(out.relative_to(kdir(agent)))
    if kind == "doc":
        d = kdir(agent) / "docs"
        d.mkdir(parents=True, exist_ok=True)
        ext = Path(name).suffix.lower()
        if ext == ".pdf":
            from pypdf import PdfReader
            text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
            out = d / (Path(name).stem + ".md")
            out.write_text(text, "utf-8")
        elif ext in (".md", ".txt"):
            out = d / name
            out.write_bytes(data)
        else:
            raise ValueError("Документы: .md, .txt или .pdf")
        return str(out.relative_to(kdir(agent)))
    raise ValueError("kind: good | bad | doc")


def delete_file(agent: str, rel: str):
    p = (kdir(agent) / rel).resolve()
    if kdir(agent).resolve() not in p.parents:
        raise ValueError("Плохой путь")
    if p.exists():
        p.unlink()
    note = p.with_suffix(".txt")
    if note.exists() and p.suffix != ".txt":
        note.unlink()


def overview(agent: str) -> dict:
    texts = []
    for name, label in TEXTS.items():
        p, src = text_path(agent, name)
        texts.append({"name": name, "label": label, "source": src,
                      "content": p.read_text("utf-8") if p.exists() else ""})
    base = kdir(agent)
    docs = [{"path": str(f.relative_to(base)), "name": f.name, "size": f.stat().st_size}
            for f in sorted((base / "docs").iterdir())] if (base / "docs").exists() else []
    return {
        "texts": texts,
        "docs": docs,
        "refs": {k: [{"path": str(p.relative_to(base)), "note": note_for(p)} for p in refs(agent, k)]
                 for k in ("good", "bad")},
    }
