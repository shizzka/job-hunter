"""Answers the user gave to employer questions, remembered per profile.

Employer questionnaires repeat the same personal questions (military ID, a
salary bank card, «работали ли с SQL»…) that no fact base answers with
confidence. When the bot cannot answer, it asks the user once in Telegram;
the answer is stored here and reused for the same question afterwards.

Only the user's own answers are stored — never model guesses.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time

MEMORY_FILE = "question_memory.json"
PENDING_FILE = "question_pending.json"
PENDING_TTL_SECONDS = 7 * 24 * 3600
MAX_OPTIONS_IN_CARD = 8


def normalize(question: str) -> str:
    text = str(question or "").casefold().replace("ё", "е")
    text = re.sub(r"\(.*?\)", " ", text)          # «(вопрос для мужчин)», «(-а)»
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def lookup(home_dir: str, question: str) -> dict | None:
    key = normalize(question)
    if not key:
        return None
    item = _read(os.path.join(home_dir, MEMORY_FILE)).get(key)
    return item if isinstance(item, dict) and item.get("answer") else None


def remember(home_dir: str, question: str, answer: str, *, control: str = "", labels: list[str] | None = None) -> dict:
    key = normalize(question)
    if not key or not str(answer or "").strip():
        raise ValueError("Пустой вопрос или ответ.")
    path = os.path.join(home_dir, MEMORY_FILE)
    data = _read(path)
    item = {"question": str(question).strip(), "answer": str(answer).strip(), "control": control,
            "labels": list(labels or []), "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    data[key] = item
    _write(path, data)
    return item


def choice_indices(item: dict, options: list[dict]) -> list[int]:
    """Map remembered option labels onto the current options (by normalized label)."""
    wanted = {normalize(label) for label in item.get("labels") or [] if normalize(label)}
    if not wanted:
        wanted = {normalize(item.get("answer", ""))}
    result = []
    for position, option in enumerate(options or []):
        if normalize(option.get("label", "")) in wanted:
            result.append(option.get("index", position))
    return result


def create_pending(home_dir: str, *, question: str, control: str, options: list[str], token: str,
                   profile_name: str, vacancy_title: str = "", company: str = "", url: str = "") -> str:
    path = os.path.join(home_dir, PENDING_FILE)
    data = _read(path)
    now = time.time()
    data = {key: value for key, value in data.items()
            if isinstance(value, dict) and now - float(value.get("created_at", 0)) < PENDING_TTL_SECONDS}
    qid = secrets.token_hex(5)
    data[qid] = {"question": question, "control": control, "options": list(options)[:MAX_OPTIONS_IN_CARD],
                 "token": token, "profile_name": profile_name, "vacancy_title": vacancy_title,
                 "company": company, "url": url, "created_at": now}
    _write(path, data)
    return qid


def find_pending(home_dir: str, *, question: str, url: str = "", vacancy_title: str = "", company: str = "") -> str:
    """qid of a live card with the same question for the same vacancy, or ""."""
    wanted = normalize(question)
    now = time.time()
    for qid, item in _read(os.path.join(home_dir, PENDING_FILE)).items():
        if not isinstance(item, dict) or now - float(item.get("created_at", 0)) >= PENDING_TTL_SECONDS:
            continue
        same_vacancy = (url and item.get("url") == url) or (
            not url and item.get("vacancy_title") == vacancy_title and item.get("company") == company)
        if same_vacancy and normalize(item.get("question", "")) == wanted:
            return qid
    return ""


def pop_pending(home_dir: str, qid: str) -> dict | None:
    path = os.path.join(home_dir, PENDING_FILE)
    data = _read(path)
    item = data.pop(str(qid), None)
    if item is not None:
        _write(path, data)
    return item if isinstance(item, dict) else None


def peek_pending(home_dir: str, qid: str) -> dict | None:
    item = _read(os.path.join(home_dir, PENDING_FILE)).get(str(qid))
    return item if isinstance(item, dict) else None
