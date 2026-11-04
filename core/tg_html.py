"""Форматирование постов Telegram (parse_mode=HTML): чистка, проверка, длина видимого текста."""
import html
import re
from html.parser import HTMLParser

ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "blockquote", "tg-spoiler"}


class _San(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack, self.errors = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.errors.append(f"тег <{tag}> Telegram не поддерживает")
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if not href.startswith(("http://", "https://", "tg://")):
                self.errors.append("у ссылки <a> нет href=\"https://…\"")
                return
            self.out.append(f'<a href="{html.escape(href, quote=True)}">')
        elif tag == "blockquote" and "expandable" in dict(attrs):
            self.out.append("<blockquote expandable>")
        else:
            self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in ALLOWED:
            return
        if tag not in self.stack:
            self.errors.append(f"лишний </{tag}>")
            return
        while self.stack:   # закрываем вложенные, которые забыли закрыть
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        self.out.append(html.escape(data, quote=False))


def from_markdown(t: str) -> str:
    """Если агент всё же написал **жирный** / __курсив__ — переводим в HTML."""
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<i>\1</i>", t, flags=re.S)
    return t


def clean(text: str) -> tuple[str, list[str]]:
    """→ (валидный HTML для Telegram, список замечаний)."""
    p = _San()
    p.feed(from_markdown(text.strip()))
    p.close()
    unclosed = list(p.stack)
    while p.stack:
        p.out.append(f"</{p.stack.pop()}>")
    errs = p.errors + ([f"не закрыт <{t}>" for t in unclosed])
    return "".join(p.out), errs


def visible(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def visible_len(text: str) -> int:
    return len(visible(text))
