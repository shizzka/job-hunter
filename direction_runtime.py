"""Bind one local resume variant to a vacancy without changing global settings.

The binding describes the requested resume. Only HH negotiation snapshots can
establish which remote resume actually reached an employer.
"""
from __future__ import annotations

import copy
import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import config

_application_resume = ContextVar("direction_application_resume", default=None)


def candidate_home(*, resume_file=None, home_dir=None) -> str:
    """Direction files are nested; personal facts still belong to the profile."""
    if getattr(config, "ACTIVE_DIRECTION", None):
        return str(home_dir or config.JOB_HUNTER_HOME)
    return os.path.dirname(resume_file or config.RESUME_FILE) or os.path.expanduser("~/.job-hunter")


def _selection_text(vacancy: dict, details: str = "") -> str:
    # First line = vacancy title (weighted), then everything known about it.
    return "\n".join(str(part) for part in (vacancy.get("title", ""), vacancy.get("snippet", ""),
                                             vacancy.get("details", ""), details) if part)


def prepare(vacancy: dict, details: str = "") -> dict | None:
    """Select once, then verify the same binding on subsequent stages.

    Queued applications carry this binding, so changing a direction, pausing a
    variant or refreshing its text cannot silently send a different resume.
    """
    direction = getattr(config, "ACTIVE_DIRECTION", None)
    bound = vacancy.get("_direction_selection")
    import directions

    home = os.path.realpath(config.JOB_HUNTER_HOME)
    current = directions.active_direction(home)
    if current != direction:
        raise ValueError("Настройки направления изменились; перезапустите поиск и оценку вакансии.")
    if not direction:
        if bound:
            raise ValueError("Направление вакансии изменилось; нужна новая оценка.")
        return None
    if bound:
        if (bound.get("profile_home") != home
                or bound.get("direction") != direction["name"]
                or bound.get("vacancy_id") != str(vacancy.get("id", ""))):
            raise ValueError("Профиль или направление вакансии изменилось; нужна новая оценка.")
        candidates = [item for item in direction["variants"]
                      if item["hh_resume_hash"] == bound.get("hh_resume_hash")
                      and item.get("status", "active") == "active"]
        if len(candidates) != 1 or any(
            candidates[0].get(key) != bound.get(key)
            for key in ("text_file", "text_sha256", "hh_resume_numeric_id")
        ):
            raise ValueError("Вариант резюме изменился; нужна новая оценка вакансии.")
    else:
        selected = directions.select_variant(direction, str(vacancy.get("id", "")),
                                             _selection_text(vacancy, details))
        bound = {
            "profile_home": home,
            "vacancy_id": str(vacancy.get("id", "")),
            "direction": direction["name"],
            "direction_title": direction["title"],
            "selection_strategy": direction["selection_strategy"],
            "selection_reason": selected["selection_reason"],
            **{key: selected.get(key) for key in (
                "hh_resume_hash", "hh_resume_numeric_id", "title", "text_file", "text_sha256",
            )},
        }
    path = Path(bound["text_file"])
    # File confinement also applies to queue records restored from disk.
    expected_dir = Path(home) / "directions" / direction["name"]
    if not path.resolve().is_relative_to(expected_dir.resolve()):
        raise ValueError("Текст варианта вне каталога направления.")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != bound["text_sha256"]:
        raise ValueError("Текст варианта изменён; обновите резюме и оцените вакансию заново.")
    raw.decode("utf-8")
    vacancy["_direction_selection"] = copy.deepcopy(bound)
    return bound


def validate_queued(vacancy: dict) -> None:
    """An old legacy score cannot approve a newly selected direction resume."""
    if not vacancy.get("_direction_selection") and getattr(config, "ACTIVE_DIRECTION", None):
        raise ValueError("Карточка оценена до выбора направления; нужна новая оценка вакансии.")
    prepare(vacancy)


def resume_text(vacancy: dict, details: str = "") -> str | None:
    bound = prepare(vacancy, details)
    if bound is None:
        return None
    raw = Path(bound["text_file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != bound["text_sha256"]:
        raise ValueError("Текст варианта изменён во время чтения.")
    return raw.decode("utf-8").strip()


def requested_variant(vacancy: dict) -> dict | None:
    bound = vacancy.get("_direction_selection")
    if not bound:
        return None
    return {"id": bound["hh_resume_hash"], "name": bound["hh_resume_hash"], "title": bound["title"]}


@contextmanager
def application_context(vacancy: dict):
    """Make the selected text available to native HH question answering."""
    token = _application_resume.set(resume_text(vacancy))
    try:
        yield
    finally:
        _application_resume.reset(token)


def application_resume() -> str | None:
    return _application_resume.get()


def bind_cover(vacancy, cover):
    """Capture the letter's verified resume before any asynchronous dispatch."""
    selection = prepare(vacancy)
    vacancy["_cover_letter_selection"] = {
        "profile_home": os.path.realpath(config.JOB_HUNTER_HOME),
        "direction": selection["direction"] if selection else "",
        "hh_resume_hash": selection["hh_resume_hash"] if selection else "",
        "sha256": hashlib.sha256(cover.encode()).hexdigest(),
    }
    return cover


def check_cover(vacancy, cover):
    selection = prepare(vacancy)
    if selection:
        import directions
        owners = {d["name"] for d in directions.list_directions(config.JOB_HUNTER_HOME)
                  for v in d["variants"] if v["hh_resume_hash"] == selection["hh_resume_hash"] or (
                      selection.get("hh_resume_numeric_id") is not None
                      and v.get("hh_resume_numeric_id") == selection["hh_resume_numeric_id"])}
        if owners != {selection["direction"]}:
            raise ValueError("Резюме относится к нескольким направлениям. Уточните выбор перед откликом.")
    binding = vacancy.get("_cover_letter_selection")
    if selection or binding:
        expected = {"profile_home": os.path.realpath(config.JOB_HUNTER_HOME),
                    "direction": selection["direction"] if selection else "",
                    "hh_resume_hash": selection["hh_resume_hash"] if selection else "",
                    "sha256": hashlib.sha256(cover.encode()).hexdigest()}
        if binding != expected:
            raise ValueError("Письмо подготовлено для другого резюме или изменилось. Подготовьте его заново.")


def trim_cover(vacancy, cover, limit):
    if len(cover) <= limit:
        return cover
    check_cover(vacancy, cover)
    return bind_cover(vacancy, cover[:limit])
