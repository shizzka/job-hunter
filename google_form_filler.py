from __future__ import annotations

import contextlib
import httpx
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
    generate_form_answers as _generate_form_answers,
)
from google_forms.extraction import _looks_like_form_info_block, extract_form_questions
from google_forms.filling import (
    _click_google_form_next,
    _click_google_form_option,
    _click_google_form_submit,
    _fill_google_form_email_consent,
    _find_google_form_button,
    _google_form_buttons,
    _google_form_page_signature,
    _google_form_preview_status,
    _google_form_required_errors,
    _has_google_form_submit_button,
    _safe_screenshot,
    _wait_google_form_submit_success,
    fill_form,
    _is_google_form_email_consent_text,
    _is_google_form_next_button_text,
    _is_google_form_submit_button_text,
    _looks_like_google_form_login_required,
    _looks_like_google_form_submit_success,
    _merge_fill_results,
    _question_signature,
    _reindex_page_questions,
)
from google_forms.urls import (
    FORM_URL_RE,
    _is_google_form_url,
    _strip_url_tail,
    extract_google_form_urls,
    normalize_google_form_url,
)
from llm_client import get_llm_client
from llm_utils import parse_llm_json
from runtime_context import RuntimePaths
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


def _runtime_paths() -> RuntimePaths:
    return RuntimePaths.from_config(config)


def _state_repository(runtime_paths: RuntimePaths | None = None) -> GoogleFormStateRepository:
    paths = runtime_paths or _runtime_paths()
    return GoogleFormStateRepository(paths.home_dir, logger=log)


def _state_path(runtime_paths: RuntimePaths | None = None) -> str:
    return str(_state_repository(runtime_paths).path)


def _load_state(runtime_paths: RuntimePaths | None = None) -> dict:
    return _state_repository(runtime_paths).load()


def _save_state(state: dict, runtime_paths: RuntimePaths | None = None) -> None:
    _state_repository(runtime_paths).save(state)


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
    return await _generate_form_answers(
        questions,
        vacancy=vacancy,
        source_message=source_message,
        client_factory=get_llm_client,
        json_parser=parse_llm_json,
        logger=log,
    )


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
