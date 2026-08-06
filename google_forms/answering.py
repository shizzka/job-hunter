from __future__ import annotations

import re

import config


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _best_option_match(options: list[str], desired: str) -> str:
    desired_norm = _norm(desired)
    if not desired_norm:
        return ""
    for option in options:
        if _norm(option) == desired_norm:
            return option
    for option in options:
        opt = _norm(option)
        if desired_norm in opt or opt in desired_norm:
            return option
    return ""


def _answers_by_index(answers: list[dict]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for answer in answers or []:
        try:
            idx = int(answer.get("index"))
        except Exception:
            continue
        out[idx] = answer
    return out


def _asks_for_telegram(question_text: str) -> bool:
    text = _norm(question_text)
    return (
        "telegram" in text
        or "телеграм" in text
        or re.search(r"(^|[^a-zа-я0-9])tg([^a-zа-я0-9]|$)", text) is not None
        or re.search(r"(^|[^a-zа-я0-9])тг([^a-zа-я0-9]|$)", text) is not None
    )


def _asks_for_resume_url(question_text: str) -> bool:
    text = _norm(question_text)
    return "резюме" in text and any(marker in text for marker in ("ссыл", "url", "link", "продубли", "прикреп"))


def _asks_for_email(question_text: str) -> bool:
    text = _norm(question_text)
    return any(marker in text for marker in ("почта", "email", "e-mail", "электронн"))


def _asks_for_phone(question_text: str) -> bool:
    text = _norm(question_text)
    if any(marker in text for marker in ("телефон", "phone")):
        return True
    return any(marker in text for marker in ("мобильный номер", "мобильного номера", "номер мобильного"))


def _contact_override_answer(idx: int, value: str) -> dict:
    return {
        "index": idx,
        "answer": value,
        "options": [],
        "skip": False,
        "confidence": "high",
        "source": "contact_override",
    }


def _apply_contact_overrides(questions: list[dict], answers: list[dict]) -> list[dict]:
    from prompt_blocks import get_candidate_contacts

    contacts = get_candidate_contacts()
    email = str(contacts.get("email") or "").strip()
    phone = str(contacts.get("phone") or "").strip()
    telegram = str(contacts.get("telegram") or "").strip()
    resume_url = str(contacts.get("resume_url") or "").strip()
    if not any((email, phone, telegram, resume_url)):
        return answers if isinstance(answers, list) else []

    originals = answers if isinstance(answers, list) else []
    answer_map = _answers_by_index(originals)
    changed = False
    for question in questions or []:
        try:
            idx = int(question.get("index"))
        except Exception:
            continue
        qtext = str(question.get("question") or "")
        if email and _asks_for_email(qtext):
            answer_map[idx] = _contact_override_answer(idx, email)
            changed = True
        elif phone and _asks_for_phone(qtext):
            answer_map[idx] = _contact_override_answer(idx, phone)
            changed = True
        elif telegram and _asks_for_telegram(qtext):
            answer_map[idx] = _contact_override_answer(idx, telegram)
            changed = True
        elif resume_url and _asks_for_resume_url(qtext):
            answer_map[idx] = _contact_override_answer(idx, resume_url)
            changed = True
    if not changed:
        return originals

    ordered = []
    emitted: set[int] = set()
    for question in questions or []:
        try:
            idx = int(question.get("index"))
        except Exception:
            continue
        if idx in answer_map:
            ordered.append(answer_map[idx])
            emitted.add(idx)
    for answer in originals:
        try:
            idx = int(answer.get("index"))
        except Exception:
            continue
        if idx not in emitted:
            ordered.append(answer)
            emitted.add(idx)
    return ordered


def _is_placeholder_answer(value: str) -> bool:
    text = _norm(value).rstrip(".:-")
    return text in {
        "не указан",
        "не указана",
        "не указано",
        "не заполнен",
        "не заполнена",
        "не заполнено",
        "нет данных",
        "n/a",
    }


def _answer_has_value(answer: dict) -> bool:
    if not isinstance(answer, dict) or answer.get("skip"):
        return False
    value = str(answer.get("answer") or "").strip()
    if value and not _is_placeholder_answer(value):
        return True
    return any(str(value or "").strip() for value in (answer.get("options") or []))


def _required_text_fallback(question_text: str) -> str:
    text = _norm(question_text)
    if any(marker in text for marker in ("фио", "имя и фам", "ваше имя")):
        return "Евгений"
    if any(marker in text for marker in ("почта", "email", "e-mail")):
        return "Готов предоставить почту в чате hh.ru"
    if any(marker in text for marker in ("телефон", "номер телефона", "phone")):
        return "Готов предоставить телефон в чате hh.ru"
    if "зарп" in text:
        baseline = str(getattr(config, "HH_AUTO_ANSWER_SALARY_BASELINE", "") or "").strip()
        if baseline:
            return f"Ориентир {baseline} ₽ на руки; итог зависит от загрузки, ответственности и формата работы."
        return "Ожидания обсуждаемы; итог зависит от загрузки, ответственности и формата работы."
    if any(marker in text for marker in ("опыт", "работали", "работал", "ситуац", "пример")):
        return "Есть релевантный QA и технический опыт; готов подробно разобрать пример на собеседовании."
    if any(marker in text for marker in ("почему", "мотивац", "интерес")):
        return "Интересна роль, где можно применить QA-подход, технический бэкграунд и ответственность за результат."
    return "Готов ответить подробнее на собеседовании."


def _is_bare_other_option(value: str) -> bool:
    text = _norm(value).rstrip(":")
    return text in {"другое", "other", "ander", "anders"}


def _required_option_fallback(options: list[str], question_text: str = "") -> list[str]:
    clean_options = [str(option or "").strip() for option in options or [] if str(option or "").strip()]
    if not clean_options:
        return []
    non_other_options = [option for option in clean_options if not _is_bare_other_option(option)]
    candidate_options = non_other_options or clean_options
    qtext = _norm(question_text)
    if "инструмент" in qtext and "мобиль" in qtext:
        for option in candidate_options:
            if "реальн" in _norm(option) and "смартф" in _norm(option):
                return [option]
    preferred_markers = (
        "qa", "тест", "гибрид", "соглас", "готов", "да", "подходит", "интерес",
    )
    for marker in preferred_markers:
        for option in candidate_options:
            if marker in _norm(option):
                return [option]
    negative_markers = ("нет", "не готов", "не подходит", "отказ", "не рассматри", "не интерес")
    for option in candidate_options:
        option_norm = _norm(option)
        if not any(marker in option_norm for marker in negative_markers):
            return [option]
    return [candidate_options[0]]


def _fallback_required_answer(question: dict) -> dict | None:
    try:
        idx = int(question.get("index"))
    except Exception:
        return None
    qtype = str(question.get("type") or "text")
    qtext = str(question.get("question") or "")
    if qtype == "text":
        return {
            "index": idx,
            "answer": _required_text_fallback(qtext),
            "options": [],
            "skip": False,
            "confidence": "low",
            "source": "required_fallback",
        }
    selected = _required_option_fallback(list(question.get("options") or []), qtext)
    if not selected:
        return None
    return {
        "index": idx,
        "answer": selected[0],
        "options": selected,
        "skip": False,
        "confidence": "low",
        "source": "required_fallback",
    }


def _apply_required_overrides(questions: list[dict], answers: list[dict]) -> list[dict]:
    originals = answers if isinstance(answers, list) else []
    answer_map = _answers_by_index(originals)
    changed = False
    for question in questions or []:
        if not question.get("required"):
            continue
        try:
            idx = int(question.get("index"))
        except Exception:
            continue
        current = answer_map.get(idx) or {}
        if _answer_has_value(current):
            continue
        fallback = _fallback_required_answer(question)
        if fallback:
            answer_map[idx] = fallback
            changed = True
    if not changed:
        return originals
    ordered = []
    emitted: set[int] = set()
    for question in questions or []:
        try:
            idx = int(question.get("index"))
        except Exception:
            continue
        if idx in answer_map:
            ordered.append(answer_map[idx])
            emitted.add(idx)
    for answer in originals:
        try:
            idx = int(answer.get("index"))
        except Exception:
            continue
        if idx not in emitted:
            ordered.append(answer)
            emitted.add(idx)
    return ordered


def _normalize_choice_answer_values(questions: list[dict], answers: list[dict]) -> list[dict]:
    originals = answers if isinstance(answers, list) else []
    question_types = {}
    for question in questions or []:
        try:
            question_types[int(question.get("index"))] = str(question.get("type") or "")
        except Exception:
            continue
    normalized = []
    changed = False
    for answer in originals:
        if not isinstance(answer, dict):
            continue
        try:
            idx = int(answer.get("index"))
        except Exception:
            normalized.append(answer)
            continue
        qtype = question_types.get(idx, "")
        if qtype not in {"radio", "checkbox"}:
            normalized.append(answer)
            continue
        values = []
        raw_options = answer.get("options") or []
        if isinstance(raw_options, list):
            values.extend(str(item).strip() for item in raw_options if str(item or "").strip())
        else:
            raw_option = str(raw_options or "").strip()
            if raw_option:
                values.append(raw_option)
        raw_answer = answer.get("answer")
        if isinstance(raw_answer, list):
            values.extend(str(item).strip() for item in raw_answer if str(item or "").strip())
        else:
            raw_answer_text = str(raw_answer or "").strip()
            if raw_answer_text:
                values.append(raw_answer_text)
        deduped = []
        seen = set()
        for value in values:
            key = _norm(value)
            if key and key not in seen:
                deduped.append(value)
                seen.add(key)
        if not deduped:
            normalized.append(answer)
            continue
        updated = dict(answer)
        updated["options"] = deduped if qtype == "checkbox" else deduped[:1]
        updated["answer"] = updated["options"] if qtype == "checkbox" else updated["options"][0]
        normalized.append(updated)
        if updated != answer:
            changed = True
    return normalized if changed else originals


def _avoid_bare_other_options(questions: list[dict], answers: list[dict]) -> list[dict]:
    answer_map = _answers_by_index(answers if isinstance(answers, list) else [])
    changed = False
    for question in questions or []:
        qtype = str(question.get("type") or "")
        if qtype not in {"radio", "checkbox"}:
            continue
        try:
            idx = int(question.get("index"))
        except Exception:
            continue
        answer = answer_map.get(idx) or {}
        selected = [str(item or "").strip() for item in (answer.get("options") or []) if str(item or "").strip()]
        if not selected:
            raw_answer = str(answer.get("answer") or "").strip()
            if raw_answer:
                selected = [raw_answer]
        if not any(_is_bare_other_option(item) for item in selected):
            continue
        question_options = list(question.get("options") or [])
        cleaned = []
        seen_cleaned = set()
        for item in selected:
            if _is_bare_other_option(item):
                continue
            match = _best_option_match(question_options, item)
            if not match or _is_bare_other_option(match):
                continue
            key = _norm(match)
            if key not in seen_cleaned:
                cleaned.append(match)
                seen_cleaned.add(key)
        if not cleaned:
            cleaned = _required_option_fallback(question_options, str(question.get("question") or ""))
        if not cleaned:
            continue
        updated = dict(answer)
        updated["options"] = cleaned if qtype == "checkbox" else cleaned[:1]
        updated["answer"] = updated["options"][0]
        updated["skip"] = False
        updated["source"] = str(updated.get("source") or "") or "other_option_guard"
        answer_map[idx] = updated
        changed = True
    if not changed:
        return answers if isinstance(answers, list) else []
    ordered = []
    emitted: set[int] = set()
    for question in questions or []:
        try:
            idx = int(question.get("index"))
        except Exception:
            continue
        if idx in answer_map:
            ordered.append(answer_map[idx])
            emitted.add(idx)
    for answer in answers if isinstance(answers, list) else []:
        try:
            idx = int(answer.get("index"))
        except Exception:
            continue
        if idx not in emitted:
            ordered.append(answer)
            emitted.add(idx)
    return ordered


def _prepare_form_answers(questions: list[dict], answers: list[dict]) -> list[dict]:
    answers = _normalize_choice_answer_values(questions, answers)
    answers = _apply_contact_overrides(questions, answers)
    answers = _apply_required_overrides(questions, answers)
    return _avoid_bare_other_options(questions, answers)
