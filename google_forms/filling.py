from __future__ import annotations

from google_forms.answering import _norm


def _looks_like_google_form_login_required(page_text: str) -> bool:
    text = _norm(page_text)
    return (
        ("log in om door te gaan" in text and "ingelogd" in text)
        or "je moet zijn ingelogd om dit formulier in te vullen" in text
        or "sign in to continue" in text
        or "you must be signed in to fill out this form" in text
        or "sign in to fill out this form" in text
        or "войдите, чтобы продолжить" in text
        or "необходимо войти" in text
    )


def _is_google_form_next_button_text(value: str) -> bool:
    text = _norm(value)
    return text in {"далее", "next", "volgende", "continuar", "weiter"}


def _is_google_form_submit_button_text(value: str) -> bool:
    text = _norm(value)
    return text in {"отправить", "submit", "verzenden", "send", "envoyer", "senden"}


def _looks_like_google_form_submit_success(page_text: str) -> bool:
    text = _norm(page_text)
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "ваш ответ записан",
            "ответ записан",
            "ответ отправлен",
            "форма отправлена",
            "отправить еще один ответ",
            "отправить ещё один ответ",
            "your response has been recorded",
            "response has been recorded",
            "submit another response",
            "je antwoord is geregistreerd",
            "je antwoord is opgenomen",
            "uw antwoord is geregistreerd",
            "uw antwoord is opgenomen",
            "verzend nog een reactie",
            "envoyer une autre réponse",
            "eine weitere antwort senden",
        )
    )


def _is_google_form_email_consent_text(value: str) -> bool:
    text = _norm(value)
    if not text:
        return False
    return (
        ("указать" in text and "электрон" in text and "почт" in text)
        or ("record" in text and "email" in text)
        or ("email" in text and "address" in text and "response" in text)
        or ("e-mailadres" in text and "antwoord" in text)
        or ("emailadres" in text and "antwoord" in text)
    )


def _google_form_preview_status(questions: list[dict], fill_result: dict, *, reached_submit: bool = True) -> tuple[bool, str]:
    filled_count = len((fill_result or {}).get("filled") or [])
    skipped = (fill_result or {}).get("skipped") or []
    skipped_indices = {int(item.get("index")) for item in skipped if str(item.get("index", "")).lstrip("-").isdigit()}
    required_skipped = [q for q in questions or [] if q.get("required") and int(q.get("index", -1)) in skipped_indices]
    if not questions:
        return False, "form questions not found"
    if required_skipped:
        return False, "required form fields were not filled"
    if filled_count <= 0:
        return False, "form detected but no fields were filled"
    if not reached_submit:
        return False, "form preview did not reach submit page"
    return True, "preview"


def _reindex_page_questions(page_questions: list[dict], *, page_index: int, start_index: int) -> list[dict]:
    out = []
    for offset, question in enumerate(page_questions or []):
        item = dict(question)
        item["page_index"] = page_index
        item["page_question_index"] = int(item.get("index") or offset)
        item["index"] = start_index + offset
        out.append(item)
    return out


def _merge_fill_results(results: list[dict]) -> dict:
    filled = []
    skipped = []
    for result in results or []:
        filled.extend(result.get("filled") or [])
        skipped.extend(result.get("skipped") or [])
    return {"filled": filled, "skipped": skipped}


def _question_signature(questions: list[dict]) -> list[str]:
    return [_norm(item.get("question") or "") for item in questions]
