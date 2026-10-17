"""Direction intent overlays; shared biography and claim boundaries stay common.

No files are written and an empty intent preserves the legacy context exactly.
"""
from __future__ import annotations

import copy
from pathlib import Path
import re

import config


def intent(profile_dir=None):
    value = getattr(config, "ACTIVE_DIRECTION", None) or {}
    if profile_dir is not None and Path(profile_dir).resolve() != Path(config.JOB_HUNTER_HOME).resolve():
        return {}
    return value if any(value.get(key) for key in ("positioning", "goal", "allowed_wording", "do_not_claim_extra")) else {}


def is_goal(label):
    label = re.sub(r"[\W_]+", " ", str(label).casefold()).strip()
    return label in {"цель", "цели", "profile note", "core profile", "canonical profile", "candidate profile", "allowed wording"} or any(
        term in label for term in ("goal", "карьерная цель", "карьерные цели", "целевая роль", "целевые роли", "цель поиска", "target roles", "positioning", "позиционирование"))


def profile_note(value=None):
    value = intent() if value is None else value
    parts = []
    if value.get("positioning"):
        parts.append(value["positioning"].strip())
    if value.get("goal"):
        parts.append("[GOAL] Цель этого направления: " + "; ".join(value["goal"]))
    if value.get("allowed_wording"):
        parts.append("Разрешённые формулировки роли: " + "; ".join(value["allowed_wording"]))
    if value.get("do_not_claim_extra"):
        parts.extend("[DO_NOT_CLAIM] " + item for item in value["do_not_claim_extra"])
    return "\n\n".join(parts)


def overlay_facts(data, value=None):
    value = intent() if value is None else value
    if not value:
        return data
    # Only intent fields are removed; limits and all biographical facts survive.
    def clean(obj):
        if isinstance(obj, dict):
            return {key: (copy.deepcopy(item) if key in {"do_not_claim", "forbidden_claims", "weak"} else clean(item)) for key, item in obj.items()
                    if not is_goal(key) and key not in {"target_roles", "allowed_wording"}}
        if isinstance(obj, list):
            return [clean(item) for item in obj]
        return copy.deepcopy(obj)
    result = clean(data)
    if value.get("goal"):
        if not isinstance(result.get("confirmed"), dict):
            result["confirmed"] = {}
        result.setdefault("confirmed", {})["target_roles"] = list(value["goal"])
    if value.get("positioning"):
        if not isinstance(result.get("inferred"), dict):
            result["inferred"] = {}
        result.setdefault("inferred", {})["positioning"] = value["positioning"]
    if value.get("allowed_wording"):
        result["allowed_wording"] = list(value["allowed_wording"])
    if value.get("do_not_claim_extra"):
        existing = result.get("do_not_claim", [])
        result["do_not_claim"] = (existing if isinstance(existing, list) else [existing]) + list(value["do_not_claim_extra"])
    return result


def markdown_parts(name, text, value=None):
    """Remove old intent structurally, retaining even bans inside old profiles."""
    from kb_retrieval import _markdown_parts, _boundary_label, _CORE_NAMES, _BOUNDARY_FILES, _TAG
    value = intent() if value is None else value
    for heading, body, mandatory in _markdown_parts(text):
        tag = _TAG.match(body)
        boundary = Path(name).name.lower() in _BOUNDARY_FILES or (tag and _boundary_label(tag.group(1))) or any(
            _boundary_label(part) for part in heading.split(" > "))
        if value and not boundary:
            if Path(name).name.lower() in _CORE_NAMES or is_goal(heading) or (tag and is_goal(tag.group(1))):
                continue
        yield heading, body, mandatory


def filter_markdown(name, text, *, profile_dir=None):
    value = intent(profile_dir)
    if not value:
        return text
    return "\n\n".join((f"### {heading}\n" if heading else "") + body
                        for heading, body, _ in markdown_parts(name, text, value))


def interview_items(items, value=None):
    value = intent() if value is None else value
    if not value:
        return items
    result = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        if str(item.get("topic", "")).strip().casefold() in {"границы", "ограничения", "limit", "do_not_claim"}:
            result.append(copy.deepcopy(item))
            continue
        name = "profile_note.md" if is_goal(item.get("topic", "")) else "interview.md"
        body = "\n\n".join(text for _, text, _ in markdown_parts(name, item["text"], value))
        if body:
            result.append({**item, "topic": "границы" if name == "profile_note.md" else item.get("topic", "общий"), "text": body})
    return result
