"""Profile-local search directions and immutable, verified resume text variants.

Reading never creates state. Invalid configuration fails closed instead of silently
falling back to a different resume or another profile's direction.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from state_store.json_store import atomic_create_text, atomic_write_json, file_lock


class DirectionError(ValueError):
    """An invalid direction or an unavailable resume variant."""


def validate_direction_name(name):
    if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None:
        raise DirectionError("Имя направления: 1–64 латинских букв, цифр, дефисов или подчёркиваний.")
    return name


def _home(home_dir):
    return Path(home_dir).expanduser().resolve()


def _inside(path, root):
    """Reject traversal and symlinks leaving the candidate's own directory."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise DirectionError("Файл направления находится вне каталога профиля.")
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise DirectionError("Символические ссылки в файлах направления запрещены.")
    return resolved


def _root(home_dir):
    home = _home(home_dir)
    return _inside(home / "directions", home)


def _path(home_dir, name):
    validate_direction_name(name)
    root = _root(home_dir)
    return _inside(root / (name + ".json"), root)


def _state_path(home_dir):
    home = _home(home_dir)
    return _inside(home / "directions_state.json", home)


def _strings(value, field):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise DirectionError(f"{field}: ожидается список непустых строк.")
    return list(value)


def _text(value, field, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise DirectionError(f"{field}: ожидается строка.")
    return value


def validate_direction(value):
    """Validate the persisted public schema without accessing any files."""
    if not isinstance(value, dict):
        raise DirectionError("Направление должно быть объектом.")
    result = copy.deepcopy(value)
    validate_direction_name(result.get("name"))
    _text(result.get("title"), "title")
    if result.get("filter_policy") not in {"qa", "generic"}:
        raise DirectionError("filter_policy: qa или generic.")
    for field in ("search_queries", "relevant_keywords", "exclude_keywords"):
        _strings(result.get(field), field)
    if "positioning" in result:
        _text(result["positioning"], "positioning", empty=True)
        if len(result["positioning"]) > 1500:
            raise DirectionError("Позиционирование: не больше 1500 символов.")
    for field in ("goal", "allowed_wording", "do_not_claim_extra"):
        if field in result:
            _strings(result[field], field)
    strategy = result.setdefault("selection_strategy", "single")
    if strategy not in {"single", "ab"}:
        raise DirectionError("selection_strategy: single или ab; closest ещё не реализован.")
    if "application_mode" in result and result["application_mode"] not in {"auto", "confirm", "preview"}:
        raise DirectionError("application_mode: auto, confirm или preview.")
    if "cover_letter_fallback" in result:
        _text(result["cover_letter_fallback"], "cover_letter_fallback", empty=True)
    variants = result.get("variants")
    if not isinstance(variants, list):
        raise DirectionError("variants: ожидается список.")
    hashes = set()
    for variant in variants:
        if not isinstance(variant, dict):
            raise DirectionError("Вариант резюме должен быть объектом.")
        resume_hash = variant.get("hh_resume_hash")
        if not isinstance(resume_hash, str) or re.fullmatch(r"[A-Za-z0-9_-]+", resume_hash) is None:
            raise DirectionError("Некорректный hash резюме HH.")
        if resume_hash in hashes:
            raise DirectionError("В направлении повторяется hash резюме HH.")
        hashes.add(resume_hash)
        identity = variant.setdefault("hh_resume_numeric_id", None)
        if identity is not None and (type(identity) is not int or identity <= 0):
            raise DirectionError("hh_resume_numeric_id: положительное число или null.")
        _text(variant.get("title"), "variant.title")
        _text(variant.get("text_file"), "text_file")
        digest = variant.get("text_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise DirectionError("Некорректный SHA256 текста резюме.")
        fetched_at = variant.get("fetched_at")
        _text(fetched_at, "fetched_at")
        try:
            fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        except ValueError:
            raise DirectionError("Некорректное время fetched_at.") from None
        if fetched.tzinfo is None:
            raise DirectionError("fetched_at должен содержать часовой пояс.")
        _strings(variant.setdefault("tags", []), "tags")
        if variant.setdefault("status", "active") not in {"active", "paused"}:
            raise DirectionError("Статус варианта: active или paused.")
    return result


def _variant_path(home_dir, name, variant):
    root = _root(home_dir)
    own = _inside(root / validate_direction_name(name), root)
    path = Path(variant["text_file"])
    if not path.is_absolute():
        path = _home(home_dir) / path
    return _inside(path, own)


def _verify_text(path, variant):
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DirectionError("Текст варианта резюме недоступен; обновите его перед откликом.") from exc
    if not text.strip() or hashlib.sha256(data).hexdigest() != variant["text_sha256"]:
        raise DirectionError("Контрольная сумма текста резюме изменилась; отклик остановлен.")
    return text


def load_direction(home_dir, name):
    path = _path(home_dir, name)
    with path.open(encoding="utf-8") as stream:
        value = validate_direction(json.load(stream))
    if value["name"] != name:
        raise DirectionError("Имя направления не совпадает с именем файла.")
    for variant in value["variants"]:
        variant["text_file"] = str(_variant_path(home_dir, name, variant))
    return value


def list_directions(home_dir):
    root = _root(home_dir)
    if not root.exists():
        return []
    return [load_direction(home_dir, path.stem) for path in sorted(root.glob("*.json"))]


def save_direction(home_dir, direction, *, texts=None):
    """Commit a draft atomically; new text uses immutable content-addressed paths.

    ``texts`` maps HH resume hashes to fetched raw text. No live text is replaced,
    including when saving the same resume hash after a new download.
    """
    if not isinstance(direction, dict):
        raise DirectionError("Направление должно быть объектом.")
    value = copy.deepcopy(direction)
    name = validate_direction_name(value.get("name"))
    path = _path(home_dir, name)
    root = _root(home_dir)
    own = _inside(root / name, root)
    texts = {} if texts is None else texts
    if not isinstance(texts, dict):
        raise DirectionError("texts: ожидается словарь hash → текст.")
    pending = {}
    if not isinstance(value.get("variants"), list):
        raise DirectionError("variants: ожидается список.")
    for variant in value["variants"]:
        if not isinstance(variant, dict):
            raise DirectionError("Вариант резюме должен быть объектом.")
        resume_hash = variant.get("hh_resume_hash")
        if not isinstance(resume_hash, str) or re.fullmatch(r"[A-Za-z0-9_-]+", resume_hash) is None:
            raise DirectionError("Некорректный hash резюме HH.")
        if resume_hash in texts:
            raw = _text(texts[resume_hash], "resume text")
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            text_path = _inside(own / (digest + ".md"), own)
            variant.update(text_file=str(text_path), text_sha256=digest,
                           fetched_at=variant.get("fetched_at") or datetime.now(timezone.utc).isoformat())
            pending[text_path] = raw
    value = validate_direction(value)
    # Validate every existing file before publishing anything.
    for variant in value["variants"]:
        text_path = _variant_path(home_dir, name, variant)
        variant["text_file"] = str(text_path)
        if text_path not in pending:
            _verify_text(text_path, variant)
    _inside(path.with_name("." + path.name + ".lock"), path.parent)
    with file_lock(path):
        for text_path, raw in pending.items():
            try:
                atomic_create_text(text_path, raw)
            except FileExistsError:
                if text_path.read_text(encoding="utf-8") != raw:
                    raise DirectionError("Существующий файл текста повреждён; сохранение остановлено.") from None
        atomic_write_json(path, value)
    return value


def active_direction(home_dir):
    path = _state_path(home_dir)
    try:
        with path.open(encoding="utf-8") as stream:
            state = json.load(stream)
    except FileNotFoundError:
        return None
    if not isinstance(state, dict) or set(state) != {"active_direction"}:
        raise DirectionError("Некорректное состояние активного направления.")
    name = state["active_direction"]
    if name is None:
        return None
    return load_direction(home_dir, name)


def set_active_direction(home_dir, name):
    if name is not None:
        load_direction(home_dir, name)
    path = _state_path(home_dir)
    _inside(path.with_name("." + path.name + ".lock"), path.parent)
    with file_lock(path):
        atomic_write_json(path, {"active_direction": name})


def select_variant(direction, vacancy_id):
    """Select deterministically among active variants and verify text before use."""
    value = validate_direction(direction)
    variants = [item for item in value["variants"] if item["status"] == "active"]
    if not variants:
        raise DirectionError("В направлении нет активных вариантов резюме.")
    strategy = value["selection_strategy"]
    if strategy == "single":
        index, reason = 0, "single:first_active"
    else:
        identity = str(vacancy_id).strip() if vacancy_id is not None else ""
        if not identity:
            raise DirectionError("Для A/B выбора необходим vacancy_id.")
        index = int(hashlib.sha256(identity.encode("utf-8")).hexdigest(), 16) % len(variants)
        reason = "ab:sha256_modulo"
    variant = copy.deepcopy(variants[index])
    path = Path(variant["text_file"])
    # Loaded/saved directions always use absolute paths scoped by their owner.
    if not path.is_absolute():
        raise DirectionError("Сначала загрузите направление из каталога профиля.")
    if path.resolve() != path:
        raise DirectionError("Путь текста резюме изменился после загрузки направления.")
    _verify_text(path, variant)
    variant["selection_reason"] = reason
    return variant


def overlay_profile(profile):
    value = active_direction(profile.home_dir)
    if value is None:
        return profile
    result = copy.deepcopy(profile)
    result._direction_base = copy.deepcopy(profile)
    result.direction = value
    result.direction_name = value["name"]
    result.filter_policy = value["filter_policy"]
    result.filter_relevant_keywords = list(value["relevant_keywords"])
    result.filter_exclude_keywords = list(value["exclude_keywords"])
    result.hh.search_queries = list(value["search_queries"])
    result.hh.secondary_resume_id = result.hh.secondary_resume_title = ""
    result.hh.tertiary_resume_id = result.hh.tertiary_resume_title = ""
    active = [item for item in value["variants"] if item["status"] == "active"]
    result.hh.primary_resume_id = active[0]["hh_resume_hash"] if active else ""
    result.hh.primary_resume_title = active[0]["title"] if active else ""
    # A paused direction must never point back to the legacy candidate text.
    result.resume_file = active[0]["text_file"] if active else str(_root(profile.home_dir) / value["name"] / "no-active-variant.md")
    if "application_mode" in value:
        result.hh.application_mode = value["application_mode"]
    return result


def select_closest_variant(direction, vacancy):
    """Reserved interface for a future closest-match selector; no LLM is called."""
    raise NotImplementedError("Стратегия closest ещё не реализована; выберите single или ab.")
