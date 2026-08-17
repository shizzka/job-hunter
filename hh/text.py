"""Shared text normalization helpers for HH workflows."""


def normalize_text(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def compact_text(value: str) -> str:
    return "".join((value or "").split()).casefold()
