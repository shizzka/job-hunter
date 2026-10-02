"""Length limits for Telegram text and well-formed Telegram HTML."""

from __future__ import annotations

import html
from html.parser import HTMLParser


def _width(text: str) -> int:
    # Conservative UTF-16 budget: astral emoji use two units, never half a pair.
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def _prefix(text: str, limit: int) -> str:
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit:
            return text[:index]
    return text


class _LimitReached(Exception):
    pass


class _HTMLPrefix(HTMLParser):
    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=False)
        self.remaining = limit
        self.parts: list[str] = []
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if self.remaining:
            self.parts.append(self.get_starttag_text())
            self.tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.tags:
            # Close the prefix in stack order, including any nested formatting.
            while self.tags:
                closing = self.tags.pop()
                self.parts.append(f"</{closing}>")
                if closing == tag:
                    break

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        prefix = _prefix(data, self.remaining)
        self.parts.append(html.escape(prefix, quote=False))
        self.remaining -= _width(prefix)
        if prefix != data:
            raise _LimitReached

    def _entity(self, raw: str) -> None:
        width = _width(html.unescape(raw))
        if width > self.remaining:
            raise _LimitReached
        self.parts.append(raw)
        self.remaining -= width

    def handle_entityref(self, name: str) -> None:
        self._entity(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._entity(f"&#{name};")

    def result(self) -> str:
        return "".join(self.parts) + "".join(f"</{tag}>" for tag in reversed(self.tags))


def limit_telegram_text(text: str, limit: int, parse_mode: str = "") -> str:
    """Keep a prefix; count decoded HTML text and never cut a tag or entity.

    This is not an HTML sanitizer: callers still need to escape dynamic values.
    Non-HTML modes retain their literal text, with a conservative UTF-16 budget.
    """
    if limit < 0:
        raise ValueError("Telegram text limit cannot be negative")
    if not limit:
        return ""
    if _width(text) <= limit:
        return text
    if (parse_mode or "").upper() != "HTML":
        return _prefix(text, limit)
    parser = _HTMLPrefix(limit)
    try:
        parser.feed(text)
        parser.close()
    except _LimitReached:
        pass
    return parser.result()
