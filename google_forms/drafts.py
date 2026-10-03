"""Manual answers kept separately from browser-generated previews."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from state_store.google_forms import (
    GoogleFormStateRepository,
    TERMINAL_STATUSES,
    SUBMITTING_STATUS,
    form_state_lock,
)
from state_store.protected import ProtectedJsonStore

TERMINAL = TERMINAL_STATUSES


def question_key(question: dict) -> str:
    value = [question.get("page_index", 0), question.get("page_question_index", question.get("index")),
             question.get("question"), question.get("type"), question.get("options", [])]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()[:24]


def _valid_edits(state: dict) -> bool:
    if not isinstance(state, dict):
        return False
    for entry in state.values():
        if not isinstance(entry, dict):
            return False
        answers = entry.get("answers", {})
        if not isinstance(answers, dict) or any(not isinstance(answer, dict) for answer in answers.values()):
            return False
        successor = entry.get("superseded_by")
        if successor is not None and not isinstance(successor, str):
            return False
    return True


def edits_store(home: str) -> ProtectedJsonStore:
    return ProtectedJsonStore(Path(home) / "google_form_edits.json", validator=_valid_edits)


def _validate_token(token: str) -> None:
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{12,32}", token):
        raise ValueError("Некорректная ссылка на черновик.")


def get_draft(home: str, token: str) -> dict:
    _validate_token(token)
    with form_state_lock(home):
        return _get_draft_unlocked(home, token)


def _get_draft_unlocked(home: str, token: str) -> dict:
    """Read eligibility while the caller owns the preview/edit workflow lock."""
    item = GoogleFormStateRepository(home).load()["items"].get(token)
    if not item:
        raise ValueError("Черновик не найден или устарел. Откройте /forms.")
    if item.get("superseded_by") or edits_store(home).load().get(token, {}).get("superseded_by"):
        raise ValueError("Есть новая проверенная версия анкеты. Откройте /forms.")
    if item.get("status") in TERMINAL | {SUBMITTING_STATUS}:
        raise ValueError("Форма уже отправлена или результат отправки требует проверки.")
    return item


def manual_answers(home: str, token: str) -> dict:
    return edits_store(home).load().get(token, {}).get("answers", {})


def supersede(home: str, token: str, new_token: str) -> None:
    _validate_token(token)
    _validate_token(new_token)
    if token == new_token:
        raise ValueError("Новая версия должна иметь отдельный token.")
    with form_state_lock(home):
        store = edits_store(home)
        item = GoogleFormStateRepository(home).load()['items'].get(token, {})
        previous = item.get('superseded_by') or store.load().get(token, {}).get("superseded_by")
        if previous == new_token:
            return
        if previous:
            raise ValueError("Есть новая проверенная версия анкеты. Откройте /forms.")
        _get_draft_unlocked(home, token)

        def mutate(state):
            state.setdefault(token, {})["superseded_by"] = new_token

        store.update(mutate)


def displayed_answers(home: str, item: dict) -> dict:
    answers = {int(a["index"]): a for a in item.get("answers", [])}
    edits = manual_answers(home, item["token"])
    for q in item.get("questions", []):
        if question_key(q) in edits:
            answers[int(q["index"])] = edits[question_key(q)]
    return answers


def needs_review(question: dict, answer: dict) -> bool:
    if answer.get("source") == "telegram_manual":
        return False
    return (not answer or answer.get("skip") or
            answer.get("source") in {"required_fallback", "other_option_guard"} or
            str(answer.get("confidence", "")).lower() in {"low", "medium"})


def answer_text(answer: dict) -> str:
    if answer.get("skip"):
        return "Оставлено пустым"
    options = answer.get("options") or []
    return ", ".join(options) if options else str(answer.get("answer") or "—")


def make_answer(question: dict, value: str) -> dict:
    value = value.strip()
    if not value or len(value) > 4000:
        raise ValueError("Нужен ответ от 1 до 4000 символов.")
    result = {"index": int(question["index"]), "answer": value, "options": [],
              "confidence": "high", "source": "telegram_manual", "skip": False}
    if value == "/skip":
        if question.get("required"):
            raise ValueError("Обязательное поле нельзя оставить пустым.")
        return {**result, "answer": "", "skip": True}
    if question.get("type") in {"radio", "checkbox", "select"}:
        options = question.get("options") or []
        values = re.split(r"[,;\n]+", value) if question.get("type") == "checkbox" else [value]
        selected = []
        for part in values:
            part = part.strip()
            match = next((o for o in options if o.casefold() == part.casefold()), None)
            if match is None and part.isdigit() and 1 <= int(part) <= len(options):
                match = options[int(part) - 1]
            if match is None:
                raise ValueError("Выберите номер или точный текст варианта из списка.")
            if match.casefold().rstrip(":") in {"другое", "other", "ander", "anders"}:
                raise ValueError("Вариант «Другое» с дополнительным полем пока заполняется по ссылке на форму.")
            if match not in selected:
                selected.append(match)
        result.update(options=selected, answer=selected[0] if len(selected) == 1 else selected)
    return result


def save_answer(home: str, token: str, index: int, value: str, user_id: int) -> dict:
    _validate_token(token)
    with form_state_lock(home):
        item = _get_draft_unlocked(home, token)
        question = next((q for q in item.get("questions", []) if int(q["index"]) == index), None)
        if not question:
            raise ValueError("Поле не найдено. Откройте черновик заново.")
        answer = make_answer(question, value)

        def mutate(state):
            entry = state.setdefault(token, {})
            entry.setdefault("answers", {})[question_key(question)] = answer
            entry.update(updated_at=int(time.time()), user_id=user_id)

        edits_store(home).update(mutate)
        return answer


def replay_answers(questions: list[dict], draft: dict, edits: dict) -> tuple[list[dict], list[dict]]:
    previous = {int(a["index"]): a for a in draft.get("answers", [])}
    saved = {question_key(q): previous.get(int(q["index"]), {}) for q in draft.get("questions", [])}
    answers, missing = [], []
    for question in questions:
        key = question_key(question)
        answer = edits.get(key) or saved.get(key)
        if answer:
            answers.append({**answer, "index": question["index"]})
        else:
            missing.append(question)
    return answers, missing
