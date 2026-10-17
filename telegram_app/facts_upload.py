"""Upload a whole facts base (.md/.txt) into the profile's knowledge/ — preview, then save."""
from __future__ import annotations

import os
import re
import time

MAX_BYTES = 400 * 1024
TARGET_NAME = "candidate_facts.md"


def summarize(text: str) -> dict:
    lines = text.splitlines()
    headings = [line.lstrip("#").strip() for line in lines if re.match(r"^\s*#{1,4}\s+\S", line)]
    tags = {tag: len(re.findall(rf"\[{tag}\]", text, flags=re.IGNORECASE))
            for tag in ("FACT", "DO_NOT_CLAIM", "LIMIT", "GOAL", "WEAK")}
    return {"chars": len(text), "lines": len(lines), "headings": headings, "tags": tags}


def preview_text(filename: str, info: dict, profile_title: str) -> str:
    tags = info["tags"]
    parts = [f"📥 Файл «{filename}» прочитан: {info['chars']} символов, разделов: {len(info['headings'])}."]
    if info["headings"]:
        shown = ", ".join(info["headings"][:8]) + ("…" if len(info["headings"]) > 8 else "")
        parts.append("Разделы: " + shown)
    marks = [f"{name}: {count}" for name, count in tags.items() if count]
    parts.append("Пометки: " + (", ".join(marks) if marks else "нет (это нормально — бот разберёт по разделам)"))
    if not tags.get("DO_NOT_CLAIM") and not tags.get("LIMIT"):
        parts.append("💡 Совет: то, чего бот НИКОГДА не должен про вас писать, отметьте строкой с [DO_NOT_CLAIM].")
    parts.append(f"\nСохранить как базу фактов профиля {profile_title}? Прежняя база сохранится копией.")
    return "\n".join(parts)


def save(knowledge_dir: str, text: str) -> str:
    """Write knowledge/candidate_facts.md atomically, keeping a timestamped copy of the old one."""
    os.makedirs(knowledge_dir, exist_ok=True)
    target = os.path.join(knowledge_dir, TARGET_NAME)
    if os.path.islink(knowledge_dir) or os.path.islink(target):
        raise ValueError("База фактов этого профиля общая (ссылка) — загрузка в неё через бота отключена.")
    if os.path.exists(target):
        os.replace(target, target + time.strftime(".bak-%Y%m%d-%H%M%S"))
    temp = target + ".tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        stream.write(text.rstrip() + "\n")
    os.replace(temp, target)
    return target
