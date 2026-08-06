from __future__ import annotations

import contextlib
import httpx
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

import config
from google_forms.answering import (
    _answer_has_value,
    _answers_by_index,
    _apply_contact_overrides,
    _apply_required_overrides,
    _asks_for_email,
    _asks_for_phone,
    _asks_for_resume_url,
    _asks_for_telegram,
    _avoid_bare_other_options,
    _best_option_match,
    _contact_override_answer,
    _fallback_required_answer,
    _is_bare_other_option,
    _is_placeholder_answer,
    _norm,
    _normalize_choice_answer_values,
    _prepare_form_answers,
    _required_option_fallback,
    _required_text_fallback,
)
from google_forms.extraction import _looks_like_form_info_block, extract_form_questions
from google_forms.urls import (
    FORM_URL_RE,
    _is_google_form_url,
    _strip_url_tail,
    extract_google_form_urls,
    normalize_google_form_url,
)
from llm_client import get_llm_client
from llm_utils import parse_llm_json
from state_store.google_forms import (
    STATE_FILENAME,
    GoogleFormStateRepository,
    new_preview_token,
)

log = logging.getLogger("google_form_filler")

CALLBACK_GOOGLE_FORM_PREVIEW = "gform_preview"
CALLBACK_GOOGLE_FORM_SUBMIT = "gform_submit"


def _safe_profile(profile_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", profile_name or "default")


def _state_repository() -> GoogleFormStateRepository:
    return GoogleFormStateRepository(config.JOB_HUNTER_HOME, logger=log)


def _state_path() -> str:
    return str(_state_repository().path)


def _load_state() -> dict:
    return _state_repository().load()


def _save_state(state: dict) -> None:
    _state_repository().save(state)


def _resolve_google_form_redirect_url(form_url: str) -> str:
    """Resolve short Google Forms links before handing them to Chromium."""
    url = str(form_url or "").strip()
    if not url:
        return ""
    host = urlparse(url).netloc.casefold()
    if host not in {"forms.gle", "goo.gl"}:
        return url
    try:
        with httpx.Client(follow_redirects=True, timeout=10.0, trust_env=False) as client:
            response = client.head(url)
            final_url = str(response.url)
            if _is_google_form_url(final_url):
                return final_url
            response = client.get(url)
            final_url = str(response.url)
            if _is_google_form_url(final_url):
                return final_url
    except Exception as exc:
        log.warning("google form redirect resolve failed for %s: %s", url, exc)
    return url


def google_form_preview_callback_data(profile_name: str, chat_id: str, message_id: str) -> str:
    return f"{CALLBACK_GOOGLE_FORM_PREVIEW}:{_safe_profile(profile_name)}:{chat_id}:{message_id}"


def google_form_submit_callback_data(profile_name: str, token: str) -> str:
    return f"{CALLBACK_GOOGLE_FORM_SUBMIT}:{_safe_profile(profile_name)}:{token}"


def parse_google_form_preview_callback_data(data: str) -> tuple[str, str, str]:
    parts = str(data or "").split(":")
    if len(parts) != 4 or parts[0] != CALLBACK_GOOGLE_FORM_PREVIEW:
        return "", "", ""
    profile_name, chat_id, message_id = (part.strip() for part in parts[1:])
    if not profile_name or not chat_id.isdigit() or not message_id.isdigit():
        return "", "", ""
    return profile_name, chat_id, message_id


def parse_google_form_submit_callback_data(data: str) -> tuple[str, str]:
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_GOOGLE_FORM_SUBMIT:
        return "", ""
    profile_name, token = (part.strip() for part in parts[1:])
    if not profile_name or not re.match(r"^[a-f0-9]{12,32}$", token):
        return "", ""
    return profile_name, token


def build_google_form_preview_markup(profile_name: str, token: str, form_url: str) -> dict:
    rows = [[{"text": "Открыть форму", "url": form_url}]]
    callback_data = google_form_submit_callback_data(profile_name, token)
    if len(callback_data.encode("utf-8")) <= 64:
        rows.append([{"text": "Отправить форму", "callback_data": callback_data}])
    return {"inline_keyboard": rows}


def _new_token(form_url: str, chat_id: str = "", message_id: str = "") -> str:
    return new_preview_token(form_url, chat_id, message_id)


def _form_id_from_url(url: str) -> str:
    match = re.search(r"/forms/d/e/([^/]+)/", str(url or ""))
    return match.group(1) if match else ""


def _same_google_form_url(left: str, right: str) -> bool:
    left_id = _form_id_from_url(left)
    right_id = _form_id_from_url(right)
    if left_id and right_id:
        return left_id == right_id
    return normalize_google_form_url(left) == normalize_google_form_url(right)


def _cached_answer_quality(answers: list[dict]) -> tuple[int, int]:
    good = 0
    fallback = 0
    for answer in answers or []:
        if answer.get("skip"):
            continue
        if str(answer.get("source") or "") == "required_fallback":
            fallback += 1
        else:
            good += 1
    return good, -fallback


async def generate_form_answers(
    questions: list[dict],
    *,
    vacancy: dict | None = None,
    source_message: str = "",
) -> list[dict]:
    try:
        from hh_client import _load_resume_text
    except Exception:
        _load_resume_text = lambda: ""
    from prompt_blocks import (
        build_contact_block,
        get_candidate_contacts,
        build_facts_block,
        build_filtered_kb_block,
        build_knowledge_base_block,
        build_profile_note_block,
        build_salary_rule_block,
    )

    resume_text = _load_resume_text()
    profile_note = build_profile_note_block()
    contacts = build_contact_block()
    facts = build_facts_block()
    salary = build_salary_rule_block()
    vacancy = vacancy or {}
    vacancy_summary = f"Должность: {vacancy.get('title','')}\nКомпания: {vacancy.get('company','')}\n"
    model = (
        getattr(config, "HH_QUESTION_MODEL", "")
        or getattr(config, "HH_CHAT_RESPONDER_MODEL", "")
        or ""
    ).strip() or config.LLM_MODEL
    client = get_llm_client()
    try:
        knowledge = await build_filtered_kb_block(vacancy_summary, client, max_sections=5, limit_chars=8000)
    except Exception as exc:
        log.warning("google form KB filter failed, fallback to full: %s", exc)
        knowledge = build_knowledge_base_block(limit_chars=8000)

    prompt = f"""Ты заполняешь Google Form от лица кандидата Евгения для отклика на работу.

Отвечай честно по резюме, фактам и базе знаний. Не выдумывай опыт, инструменты, образование, гражданство, уровень английского или даты. Если опыта нет — так и напиши. Если вопрос про зарплату — используй блок зарплатных ожиданий и правило, что итоговая зарплата зависит от загрузки, ответственности, графика и условий проекта. Если вопрос просит Telegram или ссылку на резюме, используй контактные данные кандидата ниже.

Для вопросов с вариантами выбери только точные тексты вариантов из options. Для checkbox можно выбрать несколько. Для text дай короткий конкретный ответ. Поле required=true — это поле со звёздочкой: его обязательно надо заполнить, skip=true для него запрещён. Если точного факта нет, дай честный короткий ответ вроде «нет релевантного опыта» или «готов обсудить подробнее на собеседовании», но не оставляй обязательное поле пустым. Skip допустим только для необязательных или служебных информационных блоков.

{profile_note}{contacts}{knowledge}{facts}{salary}
Контекст вакансии:
{vacancy_summary}
Сообщение HR с формой:
{source_message[:1200]}

Вопросы формы:
{json.dumps(questions, ensure_ascii=False)}

Резюме:
{resume_text[:5000]}

Верни только валидный JSON:
{{"answers":[{{"index":0,"answer":"...","options":["точный вариант"],"skip":false,"confidence":"high|medium|low"}}]}}
"""
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Ты возвращаешь только валидный JSON без markdown."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=4000,
        )
        parsed = parse_llm_json((resp.choices[0].message.content or "").strip())
    except Exception as exc:
        log.warning("google form answer generation failed: %s", exc)
        return []
    answers = parsed.get("answers") or []
    if not isinstance(answers, list):
        return []
    return _prepare_form_answers(questions, answers)


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


async def _fill_google_form_email_consent(page) -> bool:
    checkboxes = await page.locator('[role="checkbox"]:visible').element_handles()
    for checkbox in checkboxes:
        try:
            label = await checkbox.get_attribute("aria-label") or ""
        except Exception:
            label = ""
        if not label:
            try:
                label = await checkbox.inner_text()
            except Exception:
                label = ""
        if not _is_google_form_email_consent_text(label):
            continue
        try:
            checked = await checkbox.get_attribute("aria-checked") or ""
        except Exception:
            checked = ""
        if checked.casefold() == "true":
            return True
        try:
            await checkbox.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        await _click_google_form_option(checkbox)
        try:
            checked = await checkbox.get_attribute("aria-checked") or ""
        except Exception:
            checked = ""
        return checked.casefold() == "true"
    return False


async def _google_form_buttons(page) -> list[dict]:
    buttons = await page.locator('div[role="button"]:visible, button:visible').element_handles()
    out = []
    for button in buttons:
        try:
            text = await button.inner_text()
        except Exception:
            text = ""
        try:
            aria = await button.get_attribute("aria-label") or ""
        except Exception:
            aria = ""
        try:
            disabled = await button.get_attribute("aria-disabled")
            disabled_attr = await button.get_attribute("disabled")
        except Exception:
            disabled = disabled_attr = None
        label = " ".join((text or aria or "").split())
        out.append({
            "element": button,
            "text": label,
            "disabled": str(disabled or "").casefold() == "true" or disabled_attr is not None,
        })
    return out


async def _find_google_form_button(page, predicate):
    for button in await _google_form_buttons(page):
        if not button.get("disabled") and predicate(button.get("text") or ""):
            return button.get("element")
    return None


async def _has_google_form_submit_button(page) -> bool:
    return await _find_google_form_button(page, _is_google_form_submit_button_text) is not None


async def _google_form_page_signature(page) -> list[str]:
    try:
        return _question_signature(await extract_form_questions(page))
    except Exception:
        return []


async def _google_form_required_errors(page) -> list[str]:
    try:
        text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return []
    markers = (
        "это обязательный вопрос",
        "this is a required question",
        "required question",
        "dit is een verplichte vraag",
    )
    lines = []
    for line in str(text or "").splitlines():
        clean = " ".join(line.split())
        if clean and any(marker in clean.casefold() for marker in markers):
            lines.append(clean)
    return lines[:5]


async def _click_google_form_next(page) -> tuple[bool, str]:
    before_signature = await _google_form_page_signature(page)
    before_url = page.url
    button = await _find_google_form_button(page, _is_google_form_next_button_text)
    if not button:
        return False, "google form next button not found"
    try:
        await button.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    await button.click(timeout=10000)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        await page.wait_for_timeout(500)
        if await _has_google_form_submit_button(page):
            return True, "advanced"
        after_signature = await _google_form_page_signature(page)
        if after_signature and after_signature != before_signature:
            return True, "advanced"
        if page.url != before_url:
            return True, "advanced"

    errors = await _google_form_required_errors(page)
    if errors:
        return False, "google form required validation blocked next"
    return False, "google form did not advance after next"


async def _click_google_form_submit(page) -> bool:
    button = await _find_google_form_button(page, _is_google_form_submit_button_text)
    if not button:
        return False
    try:
        await button.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    await button.click(timeout=10000)
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(1500)
    return True


async def _wait_google_form_submit_success(page, *, timeout_s: float = 15.0) -> tuple[bool, str]:
    last_text = ""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            last_text = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            last_text = ""
        if _looks_like_google_form_submit_success(last_text):
            return True, last_text
        await page.wait_for_timeout(750)
    return False, last_text


async def _click_google_form_option(handle) -> None:
    try:
        await handle.click(timeout=5000)
        return
    except Exception as first_exc:
        try:
            await handle.click(timeout=5000, force=True)
            return
        except Exception:
            try:
                await handle.evaluate("el => el.click()")
                return
            except Exception:
                raise first_exc


async def fill_form(page, questions: list[dict], answers: list[dict]) -> dict:
    items = await page.locator('div[role="listitem"]:visible').element_handles()
    answer_map = _answers_by_index(answers)
    filled: list[dict] = []
    skipped: list[dict] = []
    for question in questions:
        idx = int(question.get("index", -1))
        dom_index = int(question.get("dom_index", idx))
        answer = answer_map.get(idx) or {}
        if answer.get("skip"):
            skipped.append({"index": idx, "reason": "skip requested", "question": question.get("question", "")})
            continue
        if dom_index < 0 or dom_index >= len(items):
            skipped.append({"index": idx, "reason": "dom item not found", "question": question.get("question", "")})
            continue
        item = items[dom_index]
        qtype = question.get("type") or "text"
        try:
            if qtype == "text":
                value = str(answer.get("answer") or "").strip()
                if not value:
                    skipped.append({"index": idx, "reason": "empty answer", "question": question.get("question", "")})
                    continue
                field = await item.query_selector('textarea, input[type="text"], input[type="email"], input[type="number"], input[type="url"], input[type="tel"]')
                if not field:
                    skipped.append({"index": idx, "reason": "text field not found", "question": question.get("question", "")})
                    continue
                await field.fill(value)
                actual = ""
                try:
                    actual = await field.input_value()
                except Exception:
                    actual = value
                if str(actual or "").strip() != value:
                    skipped.append({"index": idx, "reason": "text field value verification failed", "question": question.get("question", "")})
                    continue
                filled.append({"index": idx, "type": qtype, "answer": value})
                continue

            raw_options = answer.get("options") or []
            if isinstance(raw_options, list):
                desired_values = [str(value or "") for value in raw_options]
            else:
                desired_values = [str(raw_options or "")]
            if not any(value.strip() for value in desired_values):
                raw_answer = answer.get("answer")
                if isinstance(raw_answer, list):
                    desired_values = [str(value or "") for value in raw_answer]
                else:
                    desired_values = [str(raw_answer or "")]
            options = list(question.get("options") or [])
            desired_matches = []
            seen_matches = set()
            for desired in desired_values:
                match = _best_option_match(options, str(desired))
                if not match or _is_bare_other_option(match):
                    continue
                key = _norm(match)
                if key not in seen_matches:
                    desired_matches.append(match)
                    seen_matches.add(key)
                if qtype == "radio":
                    break
            selected = []
            option_handles = await item.query_selector_all('[role="radio"], [role="checkbox"]')
            if qtype == "checkbox":
                desired_norms = {_norm(value) for value in desired_matches}
                for handle in option_handles:
                    label = (await handle.get_attribute("aria-label")) or (await handle.inner_text())
                    checked = ""
                    try:
                        checked = await handle.get_attribute("aria-checked") or ""
                    except Exception:
                        checked = ""
                    if checked.casefold() == "true" and _norm(label) not in desired_norms:
                        await _click_google_form_option(handle)
            for match in desired_matches:
                for handle in option_handles:
                    label = (await handle.get_attribute("aria-label")) or (await handle.inner_text())
                    if _norm(label) == _norm(match):
                        checked = ""
                        try:
                            checked = await handle.get_attribute("aria-checked") or ""
                        except Exception:
                            checked = ""
                        if checked.casefold() != "true":
                            await _click_google_form_option(handle)
                            try:
                                checked = await handle.get_attribute("aria-checked") or ""
                            except Exception:
                                checked = ""
                        if checked.casefold() == "true":
                            selected.append(match)
                        break
                if qtype == "radio" and selected:
                    break
            if selected:
                filled.append({"index": idx, "type": qtype, "options": selected})
            else:
                skipped.append({"index": idx, "reason": "option not matched", "question": question.get("question", "")})
        except Exception as exc:
            skipped.append({"index": idx, "reason": str(exc), "question": question.get("question", "")})
    return {"filled": filled, "skipped": skipped}


async def _safe_screenshot(page, path: str) -> None:
    try:
        await page.screenshot(path=path, full_page=True)
    except Exception as exc:
        log.warning("google form screenshot failed: %s", exc)


def _question_signature(questions: list[dict]) -> list[str]:
    return [_norm(item.get("question") or "") for item in questions]


def _reuse_cached_answers(form_url: str, questions: list[dict]) -> list[dict]:
    signature = _question_signature(questions)
    if not signature:
        return []
    state = _load_state()
    candidates = []
    for item in (state.get("items") or {}).values():
        if not _same_google_form_url(item.get("form_url") or item.get("original_form_url") or "", form_url):
            continue
        answers = item.get("answers") or []
        fill_result = item.get("fill_result") or {}
        if not answers or not (fill_result.get("filled") or []):
            continue
        if _question_signature(item.get("questions") or []) != signature:
            continue
        candidates.append(item)
    if not candidates:
        return []
    latest = max(
        candidates,
        key=lambda item: (
            _cached_answer_quality(item.get("answers") or []),
            len((item.get("fill_result") or {}).get("filled") or []),
            int(item.get("created_at") or 0),
        ),
    )
    answers = latest.get("answers") or []
    log.warning(
        "google form LLM returned no answers; reusing cached answers token=%s filled=%d",
        latest.get("token") or "",
        len((latest.get("fill_result") or {}).get("filled") or []),
    )
    return answers if isinstance(answers, list) else []


async def preview_form(
    page,
    form_url: str,
    *,
    profile_name: str,
    chat_id: str = "",
    message_id: str = "",
    vacancy: dict | None = None,
    source_message: str = "",
    notify: bool = False,
) -> dict:
    original_form_url = form_url
    form_url = _resolve_google_form_redirect_url(form_url)
    await page.goto(form_url, wait_until="commit", timeout=60000)
    await page.wait_for_timeout(2500)
    page_text = ""
    try:
        page_text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        page_text = ""
    if _looks_like_google_form_login_required(page_text):
        token = _new_token(form_url, chat_id, message_id)
        shot_path = os.path.join(config.HH_STATE_DIR, f"google_form_preview_{token}.png")
        os.makedirs(os.path.dirname(shot_path), exist_ok=True)
        await _safe_screenshot(page, shot_path)
        detail = {
            "ok": False,
            "message": "google form requires Google login",
            "token": token,
            "form_url": form_url,
            "original_form_url": original_form_url,
            "page_url": page.url,
            "chat_id": chat_id,
            "message_id": message_id,
            "vacancy": vacancy or {},
            "source_message": source_message[:1500],
            "questions": [],
            "answers": [],
            "fill_result": {"filled": [], "skipped": []},
            "screenshot_path": shot_path,
            "created_at": int(time.time()),
            "status": "preview_failed_login_required",
            "profile_name": profile_name,
        }
        _state_repository().remember(token, detail, trim_expired=False)
        return detail
    token = _new_token(form_url, chat_id, message_id)
    all_questions: list[dict] = []
    all_answers: list[dict] = []
    page_results: list[dict] = []
    page_screenshots: list[str] = []
    reached_submit = False
    navigation_error = ""
    email_consent_filled = await _fill_google_form_email_consent(page)
    max_pages = 30

    for page_index in range(max_pages):
        page_questions_raw = await extract_form_questions(page)
        page_questions = _reindex_page_questions(
            page_questions_raw,
            page_index=page_index,
            start_index=len(all_questions),
        )
        if not page_questions:
            if page_index == 0:
                return {"ok": False, "message": "form questions not found", "form_url": form_url, "page_url": page.url}
            break

        answers = await generate_form_answers(page_questions, vacancy=vacancy, source_message=source_message)
        if not answers:
            answers = _reuse_cached_answers(form_url, page_questions)
        answers = _prepare_form_answers(page_questions, answers)
        fill_result = await fill_form(page, page_questions, answers)

        all_questions.extend(page_questions)
        all_answers.extend(answers)
        page_results.append({
            "page_index": page_index,
            "questions": len(page_questions),
            "fill_result": fill_result,
            "url": page.url,
        })

        page_shot = os.path.join(config.HH_STATE_DIR, f"google_form_preview_{token}_page{page_index + 1}.png")
        os.makedirs(os.path.dirname(page_shot), exist_ok=True)
        await _safe_screenshot(page, page_shot)
        page_screenshots.append(page_shot)

        if await _has_google_form_submit_button(page):
            reached_submit = True
            break
        advanced, advance_message = await _click_google_form_next(page)
        if not advanced:
            navigation_error = advance_message
            break

    fill_result = _merge_fill_results([item.get("fill_result") or {} for item in page_results])
    preview_ok, preview_message = _google_form_preview_status(
        all_questions,
        fill_result,
        reached_submit=reached_submit,
    )
    if navigation_error and not reached_submit:
        preview_ok = False
        preview_message = navigation_error
    shot_path = page_screenshots[-1] if page_screenshots else os.path.join(config.HH_STATE_DIR, f"google_form_preview_{token}.png")
    if not page_screenshots:
        os.makedirs(os.path.dirname(shot_path), exist_ok=True)
        await _safe_screenshot(page, shot_path)
    detail = {
        "ok": preview_ok,
        "message": preview_message,
        "token": token,
        "form_url": form_url,
        "original_form_url": original_form_url,
        "page_url": page.url,
        "chat_id": chat_id,
        "message_id": message_id,
        "vacancy": vacancy or {},
        "source_message": source_message[:1500],
        "questions": all_questions,
        "answers": all_answers,
        "fill_result": fill_result,
        "page_results": page_results,
        "page_screenshots": page_screenshots,
        "pages_total": len(page_results),
        "reached_submit": reached_submit,
        "navigation_error": navigation_error,
        "email_consent_filled": email_consent_filled,
        "screenshot_path": shot_path,
        "created_at": int(time.time()),
        "status": "preview" if preview_ok else "preview_failed",
        "profile_name": profile_name,
    }
    _state_repository().remember(token, detail, trim_expired=True)
    if notify and detail.get("ok"):
        await notify_form_preview(detail, profile_name=profile_name)
    return detail


async def preview_from_hh_chat(
    hh_client,
    chat_id: str,
    *,
    message_id: str = "",
    profile_name: str = "default",
    notify: bool = False,
) -> dict:
    import hh_chat_responder as cr

    if not hh_client._page:
        await hh_client.start(headless=True)
    page = hh_client._page
    await page.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1500)
    data = await cr.get_messages(page, chat_id)
    messages = data.get("messages") or []
    target = cr._find_message(messages, message_id)
    if not target:
        return {"ok": False, "message": "message not found", "chat_id": chat_id}
    form_urls = extract_google_form_urls(target.get("text") or "", target.get("links") or [])
    if not form_urls:
        for msg in reversed(messages):
            if msg.get("is_me"):
                continue
            form_urls = extract_google_form_urls(msg.get("text") or "", msg.get("links") or [])
            if form_urls:
                target = msg
                break
    if not form_urls:
        return {"ok": False, "message": "google form link not found", "chat_id": chat_id}
    form_page = await page.context.new_page()
    try:
        return await preview_form(
            form_page,
            form_urls[0],
            profile_name=profile_name,
            chat_id=chat_id,
            message_id=str(target.get("id") or message_id or ""),
            vacancy=data.get("vacancy") or {},
            source_message=(target.get("text") or ""),
            notify=notify,
        )
    finally:
        with contextlib.suppress(Exception):
            await form_page.close()


async def submit_saved_preview(hh_client, token: str, *, notify: bool = False) -> dict:
    state = _load_state()
    item = (state.get("items") or {}).get(token)
    if not item:
        return {"ok": False, "message": "google form preview token not found", "token": token}
    preview_filled = (item.get("fill_result") or {}).get("filled") or []
    if item.get("status") != "preview" or not preview_filled:
        return {
            "ok": False,
            "message": "google form preview is not ready for submit",
            "token": token,
            "form_url": item.get("form_url"),
        }
    if not hh_client._page:
        await hh_client.start(headless=True)
    page = hh_client._page
    await page.goto(item["form_url"], wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)

    saved_answers = item.get("answers") or []
    page_results: list[dict] = []
    all_questions: list[dict] = []
    reached_submit = False
    navigation_error = ""
    email_consent_filled = await _fill_google_form_email_consent(page)
    max_pages = max(1, int(item.get("pages_total") or 30))
    for page_index in range(max_pages):
        page_questions_raw = await extract_form_questions(page)
        page_questions = _reindex_page_questions(
            page_questions_raw,
            page_index=page_index,
            start_index=len(all_questions),
        )
        if not page_questions:
            break
        all_questions.extend(page_questions)
        page_fill_result = await fill_form(page, page_questions, saved_answers)
        page_results.append({
            "page_index": page_index,
            "questions": len(page_questions),
            "fill_result": page_fill_result,
            "url": page.url,
        })
        if await _has_google_form_submit_button(page):
            reached_submit = True
            break
        advanced, advance_message = await _click_google_form_next(page)
        if not advanced:
            navigation_error = advance_message
            break

    fill_result = _merge_fill_results([item.get("fill_result") or {} for item in page_results])
    submitted = False
    submit_success = False
    submit_page_text = ""
    if reached_submit:
        submitted = await _click_google_form_submit(page)
        if submitted:
            submit_success, submit_page_text = await _wait_google_form_submit_success(page)
    shot_path = os.path.join(config.HH_STATE_DIR, f"google_form_submit_{token}.png")
    os.makedirs(os.path.dirname(shot_path), exist_ok=True)
    await _safe_screenshot(page, shot_path)
    ok = bool(submitted and submit_success)
    result = {
        "ok": bool(ok),
        "submitted": submitted,
        "token": token,
        "form_url": item.get("form_url"),
        "fill_result": fill_result,
        "page_results": page_results,
        "pages_total": len(page_results),
        "reached_submit": reached_submit,
        "navigation_error": navigation_error,
        "email_consent_filled": email_consent_filled,
        "screenshot_path": shot_path,
        "submit_page_text": submit_page_text[:1500],
        "message": "submitted" if ok else "submit clicked, verification uncertain" if submitted else (navigation_error or "submit button not found"),
    }
    item["status"] = "submitted" if ok else "submit_uncertain" if submitted else "submit_failed"
    item["submitted_at"] = int(time.time())
    item["submit_result"] = result
    _save_state(state)
    if notify:
        await notify_form_submit(result)
    return result


async def notify_form_preview(detail: dict, *, profile_name: str) -> bool:
    import notifier

    questions = detail.get("questions") or []
    filled = (detail.get("fill_result") or {}).get("filled") or []
    skipped = (detail.get("fill_result") or {}).get("skipped") or []
    title = html.escape((detail.get("vacancy") or {}).get("title") or "Google Form")
    caption = (
        "<b>Подготовил заполнение Google Form</b>\n"
        f"{title}\n\n"
        f"Вопросов: {len(questions)} | заполнено: {len(filled)} | пропущено: {len(skipped)}\n"
        "Проверь скрин и отправляй только если всё выглядит нормально."
    )
    markup = build_google_form_preview_markup(profile_name, detail["token"], detail["form_url"])
    screenshot_path = detail.get("screenshot_path") or ""
    if screenshot_path and os.path.exists(screenshot_path):
        return await notifier.send_photo(screenshot_path, caption=caption, reply_markup=markup)
    return await notifier.send_message_with_markup(caption, reply_markup=markup)


async def notify_form_submit(result: dict) -> bool:
    import notifier

    status = "✅" if result.get("ok") else "⚠️"
    text = (
        f"{status} <b>Google Form submit</b>\n\n"
        f"Статус: {html.escape(result.get('message') or '')}\n"
        f"Форма: {html.escape(result.get('form_url') or '')}"
    )
    return await notifier.send_message_with_markup(text)
