"""One validation rule for owner-authored fallback letters and migration offers."""
from pathlib import Path
import unicodedata


class LetterError(ValueError):
    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


def validate(raw, *, empty=False):
    if not isinstance(raw, str):
        raise LetterError("Отправьте текст письма.", "invalid_text")
    if any(unicodedata.category(char) == "Cc" and char != "\n" for char in raw):
        raise LetterError("В письме есть недопустимые символы. Уберите их и повторите.", "control_characters")
    text = raw.strip()
    if not text and not empty:
        raise LetterError("Письмо не должно быть пустым.", "empty")
    if len(text) > 1500:
        raise LetterError("Письмо: не больше 1500 символов.", f"too_long length={len(text)}")
    return text


def legacy_offer(home):
    try:
        with (Path(home) / "cover_letter_fallback.md").open(encoding="utf-8", newline="") as stream:
            return validate(stream.read())
    except (OSError, ValueError):
        return None
