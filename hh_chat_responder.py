"""Авто-ответы на сообщения AI-помощника hh.ru в чатах с работодателями.

AI-помощник hh.ru (chatik) — это системный бот, который после отклика
задаёт квалифицирующие вопросы кандидату. Маркер: <img alt="ИИ-помощник">
с уникальным asset src=https://hhcdn.ru/file/18274603.png.

Поток:
  1. list_chats_with_ai() — открыть chatik, проскроллить, найти чаты с AI
  2. для каждого: get_messages() — получить ленту сообщений
  3. фильтр: последнее сообщение от ИИ, не от нас, не отвечено ранее
  4. generate_answer() через LLM на основе резюме + facts + истории диалога
  5. send_message() или DRY-RUN preview в TG (по флагу HH_CHAT_AUTOSEND)

Зависит от HHClient (cookies/playwright) + notifier + matcher для LLM.

CLI: ./run.sh chat-respond  (=> agent.py --chat-respond)
ENV:
  HH_CHAT_RESPONDER_ENABLED=1
  HH_CHAT_AUTOSEND=0           # 0 — dry-run + скрин в TG, 1 — реальная отправка
  HH_CHAT_MAX_REPLIES_PER_CHAT=5  # safety lock
  HH_CHAT_RESPONDER_MODEL=cogito-2.1:671b  # пустое → fallback на LLM_MODEL
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from contextvars import ContextVar
from functools import wraps
import inspect
from dataclasses import asdict
import html
import logging
import analytics
import os
from pathlib import Path
import re
import time
from typing import Any

import config
from chat_observation import ChatJournal, active_chat_journal, diagnostic_snapshot, select_chat_batch
from chat_triage import decide_chat, external_invitation, question_reason
from cover_grounding import _quote_min_similarity
from hh.ui import HHUnexpectedUI
from hh.negotiation_links import chat_link
from llm_client import get_llm_client
from llm_utils import parse_llm_json
from google_form_filler import extract_google_form_urls
from hh.chat import (
    CHATIK_CHAT_READY_SELECTOR,
    CHATIK_NAVIGATION_ATTEMPTS,
    CHATIK_NAVIGATION_TIMEOUT_MS,
    CHATIK_READY_TIMEOUT_MS,
    CHATIK_ROOT,
    dismiss_cookies_banner as _chat_dismiss_cookies_banner,
    fill_and_preview as _chat_fill_and_preview,
    find_quick_reply_button as _chat_find_quick_reply_button,
    extract_messages as _chat_extract_messages,
    get_messages as _chat_get_messages,
    get_messages_safe as _chat_get_messages_safe,
    list_chats as _chat_list_chats,
    message_matches_sent_text as _chat_message_matches_sent_text,
    messages_contain_sent_text as _chat_messages_contain_sent_text,
    normalize_sent_message_text as _chat_normalize_sent_message_text,
    open_chatik_page as _chat_open_chatik_page,
    quick_reply_choice as _chat_quick_reply_choice,
    send_message as _chat_send_message,
    reset_page_after_navigation_failure as _chat_reset_page_after_navigation_failure,
)
from runtime_context import ChatResponderLimits, RuntimePaths
from state_store.chat_responder import (
    STATE_FILENAME,
    ChatResponderStateRepository,
    get_google_form_previews,
    google_form_seen_key,
    remember_google_form_preview,
)
from chat_screening import (
    AI_ASSISTANT_AVATAR_URLS,
    AI_NAMES,
    AI_RECRUITER_TEXT_PATTERNS,
    SUSPICIOUS_SCREENING_KEYWORDS,
    SUSPICIOUS_SCREENING_TEXT_PATTERNS,
    classify_message_author as _classify_message_author,
    looks_like_ai_label as _looks_like_ai_label,
    looks_like_ai_recruiter_text as _looks_like_ai_recruiter_text,
    looks_like_suspicious_screening_text as _looks_like_suspicious_screening_text,
    normalize_ai_marker_text as _normalize_ai_marker_text,
)

log = logging.getLogger("chat_responder")


def _default_chat_dry_run() -> bool:
    return not bool(config.HH_CHAT_AUTOSEND)


def _escaped_html(value: Any, *, limit: int) -> str:
    return html.escape(str(value or "")[:limit])


def _dry_run_caption(vacancy: dict, message: dict, answer: str) -> str:
    title = _escaped_html(vacancy.get("title") or "—", limit=300)
    company = _escaped_html(vacancy.get("company") or "—", limit=200)
    question = _escaped_html(message.get("text") or "", limit=400)
    safe_answer = _escaped_html(answer, limit=800)
    return (
        "🤖 <b>Чат с AI-помощником (DRY-RUN)</b>\n"
        f"📋 {title} @ {company}\n\n"
        f"❓ <i>{question}</i>\n\n"
        f"💬 <b>Готов ответить:</b>\n{safe_answer}\n\n"
        "Чтобы включить авто-отправку: <code>HH_CHAT_AUTOSEND=1</code>"
    )


def _human_chat_button(chat_id: str, profile_name: str | None = None, *,
                       runtime_paths: RuntimePaths | None = None) -> dict:
    paths = runtime_paths or _runtime_paths()
    home = paths.home_dir
    if profile_name and profile_name != _active_profile_name():
        try:
            import profile as profile_mod
            home = profile_mod.load_profile(profile_name).home_dir
        except (OSError, ValueError):
            return {"text": "Открыть отклики", "url": "https://hh.ru/applicant/negotiations"}
    return chat_link(home, chat_id)


def _human_chat_anchor(chat_id: str, profile_name: str | None = None, *,
                       runtime_paths: RuntimePaths | None = None) -> str:
    button = _human_chat_button(chat_id, profile_name, runtime_paths=runtime_paths)
    return f"<a href='{button['url']}'>{button['text']}</a>"


def _sent_caption(chat_id: str, vacancy: dict, message: dict, answer: str, *,
                  runtime_paths: RuntimePaths | None = None) -> str:
    title = _escaped_html(vacancy.get("title") or "—", limit=300)
    company = _escaped_html(vacancy.get("company") or "—", limit=200)
    question = _escaped_html(message.get("text") or "", limit=300)
    safe_answer = _escaped_html(answer, limit=800)
    return (
        "🤖 <b>Ответил в чате</b>\n"
        f"📋 {title} @ {company}\n\n"
        f"❓ <i>{question}</i>\n\n"
        f"💬 {safe_answer}\n\n"
        f"{_human_chat_anchor(chat_id, runtime_paths=runtime_paths)}"
    )



def _is_application_only_preview(preview: str) -> bool:
    """HH lists fresh applications as chats before a real dialog exists."""
    return _normalize_ai_marker_text(preview).endswith("отклик на вакансию")


def _is_blocked_company_preview(preview: str) -> bool:
    """HH can list blocked employers as chats while the direct chat page never renders."""
    return "компания заблокирована" in _normalize_ai_marker_text(preview)


# ── State ───────────────────────────────────────────────────────────────────

def _runtime_paths() -> RuntimePaths:
    return RuntimePaths.from_config(config)


def _state_repository(runtime_paths: RuntimePaths | None = None) -> ChatResponderStateRepository:
    paths = runtime_paths or _runtime_paths()
    home = os.path.dirname(paths.resume_file) or os.path.expanduser("~/.job-hunter")
    # Use captured paths: config may already belong to another profile. Legacy
    # callers can keep a resume outside JOB_HUNTER_HOME; only direction texts
    # are nested below their owner's home and must share its chat state.
    if paths.home_dir and Path(paths.resume_file).resolve().is_relative_to((Path(paths.home_dir) / "directions").resolve()):
        home = paths.home_dir
    return ChatResponderStateRepository(home, logger=log)


def _state_path(runtime_paths: RuntimePaths | None = None) -> str:
    return str(_state_repository(runtime_paths).path)


def load_state(runtime_paths: RuntimePaths | None = None) -> dict:
    return _state_repository(runtime_paths).load()


def save_state(state: dict, runtime_paths: RuntimePaths | None = None) -> None:
    _state_repository(runtime_paths).save(state)


# ── Chat listing ────────────────────────────────────────────────────────────

async def _reset_page_after_navigation_failure(page) -> None:
    """Cancel a stuck chatik navigation before opening the next chat."""
    return await _chat_reset_page_after_navigation_failure(page, logger=log)


async def _open_chatik_page(
    page,
    url: str,
    ready_selector: str,
    *,
    settle_ms: int,
    attempts: int = CHATIK_NAVIGATION_ATTEMPTS,
    ready_timeout_ms: int = CHATIK_READY_TIMEOUT_MS,
    log_failures: bool = True,
) -> None:
    """Open a chatik page and retry once after resetting a stuck tab."""
    return await _chat_open_chatik_page(
        page,
        url,
        ready_selector,
        settle_ms=settle_ms,
        attempts=attempts,
        ready_timeout_ms=ready_timeout_ms,
        log_failures=log_failures,
        navigation_timeout_ms=CHATIK_NAVIGATION_TIMEOUT_MS,
        reset_page=_reset_page_after_navigation_failure,
        logger=log,
    )


async def list_chats(page) -> list[dict]:
    """Открыть chatik root и вернуть свежие чаты с metadata."""
    return await _chat_list_chats(
        page,
        chatik_root=CHATIK_ROOT,
        open_page=_open_chatik_page,
    )


# ── Message extraction ──────────────────────────────────────────────────────

async def _extract_messages(page) -> dict[str, Any]:
    """Read messages from the currently open chat without navigating."""
    return await _chat_extract_messages(
        page,
        classify_author=_classify_message_author,
    )


async def get_messages(page, chat_id: str) -> dict[str, Any]:
    """Открыть chat прямой URL, вернуть messages+vacancy."""
    data = await _chat_get_messages(
        page,
        chat_id,
        chatik_root=CHATIK_ROOT,
        ready_selector=CHATIK_CHAT_READY_SELECTOR,
        open_page=_open_chatik_page,
        extract_current_messages=_extract_messages,
    )
    journal = active_chat_journal.get()
    if journal is not None:
        data["_journal_snapshot_id"] = journal.thread(str(chat_id), data)
    return data


async def get_messages_safe(
    page,
    chat_id: str,
    *,
    attempts: int = 1,
    ready_timeout_ms: int = 5000,
    settle_ms: int = 800,
) -> dict[str, Any]:
    """Best-effort chat read for scanners that should skip broken/empty chats quickly."""
    return await _chat_get_messages_safe(
        page,
        chat_id,
        attempts=attempts,
        ready_timeout_ms=ready_timeout_ms,
        settle_ms=settle_ms,
        chatik_root=CHATIK_ROOT,
        ready_selector=CHATIK_CHAT_READY_SELECTOR,
        open_page=_open_chatik_page,
        extract_current_messages=_extract_messages,
        reset_page=_reset_page_after_navigation_failure,
    )


# ── LLM ─────────────────────────────────────────────────────────────────────

_llm_client = None


def _get_llm_client():
    global _llm_client
    if _llm_client is None:
        _llm_client = get_llm_client()
    return _llm_client


def _format_dialog(messages: list[dict], take_last: int = 10) -> str:
    tail = messages[-take_last:]
    lines = []
    for m in tail:
        if m.get("is_ai"):
            tag = "AI"
        elif m.get("is_ai_suspect"):
            tag = m.get("author") or "Possible AI/HR"
        elif m.get("is_me"):
            tag = "Я"
        else:
            tag = m.get("author") or "Other"
        text = (m.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        lines.append(f"[{tag}] {text}")
    return "\n".join(lines)


async def generate_answer(
    messages: list[dict],
    vacancy: dict,
    resume_text: str,
    *,
    question_message: dict[str, Any] | None = None,
    question_kind: str = "AI-помощника",
    alternative: bool = False,
    diagnostics: dict | None = None,
    quote_min_similarity: float = 1.0,
) -> str | None:
    """Сгенерировать ответ на вопрос AI-помощника или approved suspicious HR message."""
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics['reason'] = 'missing_question'
    if not messages:
        return None
    target_message = question_message or next((m for m in reversed(messages) if m.get("is_ai")), None)
    if not target_message:
        return None
    question = target_message.get("text", "").strip()
    if not question:
        return None
    # Контексты
    from prompt_blocks import (
        build_profile_note_block,
        build_facts_block,
        build_salary_rule_block,
        build_knowledge_base_block,
        build_filtered_kb_block,
    )
    from answer_grounding import (
        capture_candidate, current_candidate, current_answer_client,
        verify_answers, retrieve_candidate,
    )
    snapshot = current_candidate() or capture_candidate(resume_text)
    vacancy_summary = f"Должность: {vacancy.get('title','')}\nКомпания: {vacancy.get('company','')}\n"
    retrieval_query = (question + '\n' + vacancy_summary
                       + f"{vacancy.get('description', '')}\n{vacancy.get('snippet', '')}")
    snapshot = retrieve_candidate(snapshot, retrieval_query, limit_chars=12000)
    resume_text = snapshot.resume
    profile_note, facts, salary = snapshot.profile_note, snapshot.facts, snapshot.salary
    client = current_answer_client() or _get_llm_client()
    model = snapshot.models['HH_CHAT_RESPONDER_MODEL'].strip() or snapshot.models['LLM_MODEL']
    deterministic_answer = _deterministic_chat_answer(question)
    if deterministic_answer:
        supported = await verify_answers([{'index': 0, 'question': question, 'answer': deterministic_answer}],
                                         snapshot.sources, client, model, constraints=snapshot.fact_constraints,
                                         diagnostics=diagnostics, quote_min_similarity=quote_min_similarity)
        return deterministic_answer if 0 in supported else None
    # Legacy selection remains available only for operations captured flag-off.
    try:
        knowledge = snapshot.knowledge if snapshot.knowledge_index or current_candidate() else await build_filtered_kb_block(
            vacancy_summary, client, max_sections=6, limit_chars=12000,
        )
    except Exception as exc:
        log.warning("filtered KB selection failed, fallback to captured: %s", type(exc).__name__)
        knowledge = snapshot.knowledge

    vacancy_block = ""
    if vacancy.get("title") or vacancy.get("company"):
        vacancy_block = (
            f"Контекст вакансии:\n"
            f"- Должность: {vacancy.get('title', '—')}\n"
            f"- Компания: {vacancy.get('company', '—')}\n\n"
        )

    dialog_block = _format_dialog(messages, take_last=10)

    alternative_instruction = (
        "\nНужен альтернативный вариант: сформулируй ответ другими словами и с другим порядком фактов, "
        "но используй только подтверждённые факты.\n"
        if alternative else ""
    ) + _language_instruction(question)
    prompt = f"""Ты отвечаешь в чате hh.ru от лица кандидата на вопрос работодателя.

Тип вопроса: {question_kind}. Если это похоже на автоматический HR-скрининг, отвечай так же конкретно, как AI-помощнику, но без упоминания, что собеседник является ботом. На вопрос о зарплатных ожиданиях отвечай по блоку зарплатных ожиданий ниже.

Опирайся на канонический профиль, структурированные факты и резюме. Контекст вакансии не является фактом о кандидате. Если данных нет — status=skip для ручного уточнения. Отсутствие данных НЕ означает «нет опыта», «не работал» или «изучаю». Не выдумывай инструменты/языки/опыт.

Если AI спрашивает количество лет опыта в конкретной технологии — назови только явно подтверждённое значение или верни skip.

Длина ответа: 2-4 предложения, конкретика. Без приветствий («Здравствуйте» — не нужно, мы уже в диалоге). Без шаблонных оборотов «активно», «успешно», «эффективно», «глубокий опыт».

{profile_note}{knowledge}{facts}{salary}{vacancy_block}История диалога (последние 10 реплик):
{dialog_block}
{alternative_instruction}

Текущий вопрос:
"{question}"

Резюме кандидата (для деталей):
{resume_text[:4000]}

Верни ТОЛЬКО валидный JSON, без markdown. Первый символ `{{`, последний `}}`. Формат:
{{
  "status": "answer" | "skip",
  "answer": "<текст ответа на вопрос AI>"
}}

Правила:
- status=skip только если совсем нельзя ответить (например AI предлагает конкретную дату собеседования или просит принять оффер — это требует решения человека).
- НЕ начинай ответ с приветствия.
- НЕ объясняй что ты ассистент.
- Отвечай в первом лице без предположений об имени или профессии кандидата."""

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Ты отвечаешь строго JSON. Никакого текста до или после JSON-объекта."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.55 if alternative else 0.3,
            max_tokens=600,
        )
        if resp.choices[0].finish_reason != 'stop':
            diagnostics['reason'] = 'generation_incomplete'
            return None
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        log.warning("LLM chat-answer failed: %s", type(exc).__name__)
        diagnostics['reason'] = 'model_unavailable'
        return None

    try:
        parsed = parse_llm_json(raw)
    except Exception:
        diagnostics['reason'] = 'invalid_model_answer'
        return None
    if not isinstance(parsed, dict) or parsed.get("status") != "answer":
        diagnostics['reason'] = ('needs_profile_details' if isinstance(parsed, dict)
                                 and parsed.get('status') == 'skip' else 'invalid_model_answer')
        return None
    answer = parsed.get("answer", "")
    if not isinstance(answer, str):
        diagnostics['reason'] = 'invalid_model_answer'
        return None
    answer = answer.strip()
    if answer and _looks_like_screening_form_artifact(answer) and not _is_screening_form_question(question):
        diagnostics['reason'] = 'unrelated_answer'
        log.warning(
            "LLM chat-answer dropped form artifact for non-form question: chars=%d",
            len(question),
        )
        return None
    if not answer:
        diagnostics['reason'] = 'empty_answer'
        return None
    supported = await verify_answers([{'index': 0, 'question': question, 'answer': answer}],
                                     snapshot.sources, client, model, constraints=snapshot.fact_constraints,
                                     diagnostics=diagnostics, quote_min_similarity=quote_min_similarity)
    return answer if 0 in supported else None


# ── Send ────────────────────────────────────────────────────────────────────

async def _dismiss_cookies_banner(page) -> None:
    """Закрыть баннер «Мы используем файлы cookie» если есть — чтобы не перекрывал скрин."""
    return await _chat_dismiss_cookies_banner(page)


def _normalize_sent_message_text(value: str) -> str:
    return _chat_normalize_sent_message_text(value)


def _message_matches_sent_text(actual: str, expected: str) -> bool:
    return _chat_message_matches_sent_text(
        actual,
        expected,
        normalize_text=_normalize_sent_message_text,
    )


def _messages_contain_sent_text(messages: list[dict], expected: str) -> bool:
    return _chat_messages_contain_sent_text(
        messages,
        expected,
        message_matches=_message_matches_sent_text,
    )


def _is_study_certificate_question(text: str) -> bool:
    normalized = _normalize_ai_marker_text(text)
    if "справк" not in normalized:
        return False
    return any(token in normalized for token in ("обучени", "учеб", "учебы", "учебы", "учебн", "стажировк"))


def _is_screening_form_question(text: str) -> bool:
    normalized = _normalize_ai_marker_text(text)
    if not normalized:
        return False
    form_nouns = r"(?:форм\w*|анкет\w*|опросник\w*|опрос\w*)"
    return any(
        re.search(pattern, normalized)
        for pattern in (
            rf"\b(?:заполн\w*|прой\w*|пройти|отправ\w*)\s+(?:\w+\s+){{0,3}}{form_nouns}\b",
            rf"\b{form_nouns}\s+(?:\w+\s+){{0,3}}(?:заполн\w*|прой\w*|пройти|отправ\w*)\b",
            r"\bответ\w*\s+на\s+(?:несколько\s+|коротк\w+\s+|дополнительн\w+\s+){0,2}вопрос\w*\b",
        )
    )


def _looks_like_screening_form_artifact(answer: str) -> bool:
    normalized = _normalize_ai_marker_text(answer)
    if not normalized:
        return False
    artifact_patterns = (
        r"\bготов\w*\s+прой\w*\s+(?:коротк\w+\s+)?форм\w*\b",
        r"\bзаполн\w*\s+вопрос\w*\s+по\s+опыт\w*\s+и\s+навык\w*\b",
        r"\bготов\w*\s+заполн\w*\s+(?:коротк\w+\s+)?(?:форм\w*|анкет\w*)\b",
    )
    return any(re.search(pattern, normalized) for pattern in artifact_patterns)


def _deterministic_chat_answer(question: str) -> str | None:
    if _is_study_certificate_question(question):
        return (
            "Справку с места учебы как действующий студент предоставить не смогу, "
            "сейчас я не учусь. При этом у меня есть техническое образование, "
            "документы об образовании могу предоставить. Если для стажировки "
            "критична именно справка от текущего учебного заведения, лучше сразу "
            "это уточнить."
        )
    if _is_screening_form_question(question):
        return (
            "Да, готов пройти короткую форму. Заполню вопросы по опыту и навыкам; "
            "если понадобится уточнение, отвечу отдельно."
        )
    return None


def _quick_reply_choice(text: str) -> str:
    return _chat_quick_reply_choice(
        text,
        normalize_text=_normalize_sent_message_text,
    )


async def _find_quick_reply_button(page, choice: str):
    return await _chat_find_quick_reply_button(page, choice)


async def fill_and_preview(
    page,
    chat_id: str,
    text: str,
    *,
    runtime_paths: RuntimePaths | None = None,
) -> dict:
    """Перейти в chat, набрать текст в input. НЕ отправлять.
    Возвращает {filled, screenshot_path}."""
    paths = runtime_paths or _runtime_paths()
    return await _chat_fill_and_preview(
        page,
        chat_id,
        text,
        state_dir=paths.hh_state_dir,
        chatik_root=CHATIK_ROOT,
        ready_selector=CHATIK_CHAT_READY_SELECTOR,
        now=time.time,
        path_join=os.path.join,
        open_page=_open_chatik_page,
        dismiss_cookies=_dismiss_cookies_banner,
        choose_quick_reply=_quick_reply_choice,
        find_quick_reply=_find_quick_reply_button,
    )


async def send_message(
    page,
    chat_id: str,
    text: str,
    *,
    runtime_paths: RuntimePaths | None = None,
    before_send=None,
    on_no_action=None,
) -> bool:
    """Полная отправка: перейти, набрать, нажать Send."""
    paths = runtime_paths or _runtime_paths()

    async def fill_preview(current_page, current_chat_id: str, current_text: str) -> dict:
        return await fill_and_preview(
            current_page,
            current_chat_id,
            current_text,
            runtime_paths=paths,
        )

    return await _chat_send_message(
        page,
        chat_id,
        text,
        fill_preview=fill_preview,
        find_quick_reply=_find_quick_reply_button,
        extract_current_messages=_extract_messages,
        messages_contain=_messages_contain_sent_text,
        logger=log,
        **({"before_send": before_send} if before_send is not None else {}),
        **({"on_no_action": on_no_action} if on_no_action is not None else {}),
    )


# ── Approval lane for suspicious HR messages ───────────────────────────────

def _active_profile_name() -> str:
    try:
        import profile as profile_mod
        return str(getattr(profile_mod.active(), "name", "") or "default")
    except Exception:
        return os.getenv("JOB_HUNTER_DEFAULT_PROFILE", "default") or "default"


def _chat_callback_data(action: str, profile_name: str, chat_id: str, message_id: str) -> str:
    safe_profile = re.sub(r"[^a-zA-Z0-9_.-]+", "_", profile_name or "default")
    return f"{action}:{safe_profile}:{chat_id}:{message_id}"


def chat_ai_callback_data(profile_name: str, chat_id: str, message_id: str) -> str:
    return _chat_callback_data("chat_ai", profile_name, chat_id, message_id)


def chat_send_callback_data(profile_name: str, chat_id: str, message_id: str) -> str:
    return _chat_callback_data("chat_send", profile_name, chat_id, message_id)


def chat_ai_manual_callback_data(profile_name: str, chat_id: str, message_id: str) -> str:
    return _chat_callback_data("chat_ai_any", profile_name, chat_id, message_id)


def chat_ai_alternative_callback_data(profile_name: str, chat_id: str, message_id: str) -> str:
    return _chat_callback_data("chat_ai_alt", profile_name, chat_id, message_id)


def chat_ai_manual_alternative_callback_data(profile_name: str, chat_id: str, message_id: str) -> str:
    return _chat_callback_data("chat_ai_any_alt", profile_name, chat_id, message_id)


def chat_manual_send_callback_data(profile_name: str, chat_id: str, message_id: str) -> str:
    return _chat_callback_data("chat_send_any", profile_name, chat_id, message_id)


def build_suspicious_chat_reply_markup(profile_name: str, chat_id: str, message_id: str) -> dict:
    rows = [[_human_chat_button(chat_id, profile_name)]]
    callback_data = chat_ai_callback_data(profile_name, chat_id, message_id)
    if len(callback_data.encode("utf-8")) <= 64:
        rows.append([{"text": "Ответить с ИИ", "callback_data": callback_data}])
    return {"inline_keyboard": rows}


def build_chat_answer_preview_markup(
    profile_name: str,
    chat_id: str,
    message_id: str,
    *,
    allow_any: bool = False,
    draft_revision: str = "",
) -> dict:
    rows = [[_human_chat_button(chat_id, profile_name)]]
    send_id = message_id + ("~" + draft_revision if draft_revision else "")
    callback_data = (
        chat_manual_send_callback_data(profile_name, chat_id, send_id)
        if allow_any
        else chat_send_callback_data(profile_name, chat_id, send_id)
    )
    alternative_data = (
        chat_ai_manual_alternative_callback_data(profile_name, chat_id, message_id)
        if allow_any else chat_ai_alternative_callback_data(profile_name, chat_id, message_id)
    )
    actions = []
    if len(alternative_data.encode("utf-8")) <= 64:
        actions.append({"text": "🔄 Другой вариант", "callback_data": alternative_data})
    if len(callback_data.encode("utf-8")) <= 64:
        actions.append({"text": "✅ Отправить", "callback_data": callback_data})
    if actions:
        rows.append(actions)
    return {"inline_keyboard": rows}


async def notify_suspicious_screening_message(
    notifier,
    *,
    chat_id: str,
    vacancy: dict,
    message: dict,
    profile_name: str | None = None,
) -> bool:
    if notifier is None:
        return False
    profile_name = profile_name or _active_profile_name()
    message_id = str(message.get("id") or "")
    question = html.escape((message.get("text") or "").strip()[:300])
    author = html.escape((message.get("author") or "HR").strip()[:120])
    title = html.escape((vacancy.get("title") or "—").strip()[:160])
    company = html.escape((vacancy.get("company") or "—").strip()[:160])
    text = (
        "<b>Обнаружено странное сообщение</b>\n"
        "Похоже на автоматический HR-скрининг, но явного AI-маркера нет. "
        "Автоответ не отправляю без подтверждения.\n\n"
        f"<b>Чат:</b> {title} @ {company}\n"
        f"<b>Автор:</b> {author}\n\n"
        f"<i>{question}</i>"
    )
    markup = build_suspicious_chat_reply_markup(profile_name, chat_id, message_id)
    return await notifier.send_message_with_markup(text, reply_markup=markup)


def _language_instruction(question: str) -> str:
    """Answer in the employer's language (B14): an English message gets an English reply."""
    latin = len(re.findall(r"[A-Za-z]", question or ""))
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", question or ""))
    if latin >= 20 and latin > 2 * cyrillic:
        return ("\nСообщение работодателя на английском: напиши ответ на английском языке. Факты те же — "
                "только подтверждённые, переводи их точно, без добавлений.\n")
    return ""


def _chat_owner(chat_id):
    """Informational lookup only; fact-bearing operations use bind_candidate."""
    try:
        import chat_context
        import config
        return chat_context.resolve(config.JOB_HUNTER_HOME, str(chat_id))
    except Exception as exc:
        log.warning("chat owner lookup failed: %s", type(exc).__name__)
        return None


def _active_direction_name() -> str:
    try:
        import config
        return str((getattr(config, "ACTIVE_DIRECTION", None) or {}).get("name") or "")
    except Exception:
        return ""


def _latest_topic_states() -> dict[str, str]:
    """chat id -> lastState (RESPONSE / DISCARD / INVITATION / INTERVIEW…) from the profile's
    latest negotiation snapshot; empty when there is none (then nothing changes)."""
    return {chat: row["state"] for chat, row in _latest_topics().items() if row.get("state")}


def _latest_topics() -> dict[str, dict]:
    """chat id -> {state, company, title} from the profile's latest negotiation snapshot."""
    topics: dict[str, dict] = {}
    try:
        import config
        path = os.path.join(config.JOB_HUNTER_HOME, "negotiation_snapshots.jsonl")
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                if '"chatId"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("chatId"):
                    topics[str(row["chatId"])] = {"state": str(row.get("lastState") or ""),
                                                 "company": str(row.get("company_name") or ""),
                                                 "title": str(row.get("vacancy_name") or "")}
    except OSError:
        pass
    return topics


# Closing messages that need no answer (live 18.10: rejections in archived chats and «свяжемся
# при положительном решении» were sent as «Вопрос HR» cards with «Ответить с ИИ»).
_REJECTION_TEXT = re.compile(
    r"не готовы (?:вас )?пригласить|не можем (?:вам )?предложить|к сожалению[^.!?]{0,80}"
    r"(?:не готов|отказ|не подход|не смож|выбрали другого|приняли решение)|"
    r"(?:приняли|принято) решение[^.!?]{0,60}(?:в пользу|не в вашу|отказ|другого кандидата)|"
    r"вакансия (?:уже )?закрыта|позиция (?:уже )?закрыта", re.I)
_WAITING_TEXT = re.compile(
    r"(?:при положительном решении|в случае положительного решения|если (?:ваша )?кандидатура)[^.!?]{0,60}"
    r"свяж|(?:мы )?(?:обязательно )?рассмотрим (?:ваше )?резюме[^.!?]{0,80}свяж", re.I)


def _closing_kind(text: str) -> str:
    """«rejection» / «waiting» for messages that end the exchange; "" otherwise."""
    value = " ".join(str(text or "").split())
    if _REJECTION_TEXT.search(value):
        return "rejection"
    if _WAITING_TEXT.search(value) and "?" not in value:
        return "waiting"
    return ""


def _notifier_module():
    try:
        import notifier
        return notifier
    except Exception:
        return None


async def _notify_rejections(notifier, rejections: list[dict], waiting: list[dict] | None = None) -> bool:
    waiting = waiting or []
    if notifier is None or not (rejections or waiting):
        return False

    def block(items):
        lines = [f"• {_escaped_html(item['company'], limit=80)} — {_escaped_html(item['title'], limit=90)}"
                 for item in items[:20]]
        return "\n".join(lines) + (f"\n…и ещё {len(items) - 20}" if len(items) > 20 else "")

    parts = []
    if rejections:
        parts.append(f"📭 <b>Отказы в чатах: {len(rejections)}</b>\n" + block(rejections))
    if waiting:
        parts.append(f"⏳ <b>Ответят сами (обещали связаться): {len(waiting)}</b>\n" + block(waiting))
    return await notifier.send_message("\n\n".join(parts) + "\n\nОтвечать на них не нужно.")


async def notify_hr_message(notifier, *, chat_id, vacancy, message, profile_name, invitation=None):
    """Notification only. AI generation stays behind the existing manual callback."""
    if notifier is None:
        return False
    title = _escaped_html(vacancy.get("title") or "—", limit=120)
    company = _escaped_html(vacancy.get("company") or "—", limit=120)
    # Bound *escaped* excerpts as well, so unusual HTML characters cannot exceed Telegram's limit.
    excerpt = html.escape((message.get("text") or "").strip()[:900])
    if len(excerpt) > 1800:
        excerpt = html.escape((message.get("text") or "").strip()[:300])
    rows = [[_human_chat_button(chat_id, profile_name)]]
    if invitation:
        heading = "Приглашение пройти: " + invitation["kind"]
        # Long URLs belong in buttons, not in Telegram's HTML text limit.
        rows += [[{"text": f"Ссылка {i + 1}", "url": url}]
                 for i, url in enumerate(invitation["urls"])]
    else:
        heading = "Вопрос HR — требуется подтверждение"
        callback = chat_ai_manual_callback_data(profile_name, chat_id, str(message.get("id") or ""))
        if len(callback.encode()) <= 64:
            rows.append([{"text": "Ответить с ИИ", "callback_data": callback}])
    return await notifier.send_message_with_markup(
        f"<b>{_escaped_html(heading, limit=100)}</b>\n<b>Чат:</b> {title} @ {company}\n\n<i>{excerpt}</i>",
        reply_markup={"inline_keyboard": rows},
    )


async def _notify_ownership_review(repository, notifier, refusal, *, message_id, profile_name, vacancy=None):
    """A durable review notice without any candidate facts or generation action."""
    if notifier is None:
        return False
    chat_id = str(refusal["chat_id"])
    # Reissue old text-only refusals once with the new owner-selection action.
    with repository.attempt(chat_id, "notice", "ownership-choice:" + str(message_id)) as attempt:
        if not attempt:
            return False
        repository.mark_acting(chat_id, attempt)
        vacancy = vacancy or {}
        title = _escaped_html(vacancy.get("title") or "—", limit=120)
        from chat_owner_review import create_review
        review_token = create_review(repository.path.parent, chat_id, message_id, profile_name)
        choose = {"text": "Выбрать профиль и резюме", "callback_data": f"cbo:{profile_name}:{review_token}"}
        rows = [[_human_chat_button(chat_id, profile_name)]]
        if len(choose["callback_data"].encode()) <= 64:
            rows.append([choose])
        text = ("<b>Чат требует вашей проверки</b>\n" + title + "\n\n"
                + html.escape(refusal["message"]))
        try:
            delivered = await notifier.send_message_with_markup(text, reply_markup={"inline_keyboard": rows})
        except Exception as exc:
            log.warning("chat ownership notice failed: %s", type(exc).__name__)
            delivered = False
        if not repository.finish(chat_id, attempt, "completed" if delivered else "uncertain"):
            raise RuntimeError("Chat ownership notice completion lost")
        return bool(delivered)


def _find_message(messages: list[dict], message_id: str) -> dict | None:
    if message_id:
        found = next((m for m in messages if str(m.get("id") or "") == str(message_id)), None)
        if found:
            return found
    return messages[-1] if messages else None


def _chat_candidate_kind(message: dict[str, Any]) -> str:
    if message.get("is_ai"):
        return "ai"
    if message.get("is_ai_suspect"):
        return "suspect"
    return "manual"


def _chat_candidate_kind_label(kind: str) -> str:
    if kind == "ai":
        return "AI"
    if kind == "suspect":
        return "похоже на AI"
    return "HR"


def _short_chat_text(value: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


@analytics.chat_context
async def list_reply_candidates(
    hh_client,
    *,
    limit: int = 8,
    max_scan: int = 25,
    runtime_paths: RuntimePaths | None = None,
) -> dict:
    # Return recent incoming HH chat messages that can be answered from Telegram buttons.
    from direction_views import DirectionView
    view = DirectionView(config.JOB_HUNTER_HOME)
    paths = runtime_paths or _runtime_paths()
    if not hh_client._page:
        await hh_client.start(headless=True)
    page = hh_client._page
    await page.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1500)

    state = load_state(paths)
    candidates: list[dict[str, Any]] = []
    summary = {
        "ok": True,
        "chats_scanned": 0,
        "chats_read": 0,
        "max_scan": max_scan,
        "limit": limit,
        "read_failures": 0,
        "skipped_closing": 0,
        "skipped_other_profile": 0,
        "skipped_other_direction": 0,
        "ownership_reviews": [],
        "direction_title": view.title,
        "candidates": candidates,
    }
    topics = _latest_topics()
    try:
        chats = await list_chats(page)
    except Exception as exc:
        summary.update({
            "ok": False,
            "message": "chatik list is unavailable",
            "error": str(exc)[:500],
            "url": getattr(page, "url", ""),
        })
        return summary
    summary["chats_scanned"] = len(chats)

    for chat in chats[: max(1, max_scan)]:
        if len(candidates) >= max(1, limit):
            break
        chat_id = str(chat.get("chat_id") or "")
        if not chat_id:
            continue
        preview_text = chat.get("preview") or ""
        if _is_application_only_preview(preview_text) or _is_blocked_company_preview(preview_text):
            continue

        try:
            data = await get_messages(page, chat_id)
            summary["chats_read"] += 1
        except HHUnexpectedUI:
            raise
        except Exception as exc:
            log.warning("candidate get_messages(%s) failed: %s", chat_id, type(exc).__name__)
            summary["read_failures"] += 1
            continue

        messages = data.get("messages") or []
        last = messages[-1] if messages else None
        if not last or last.get("is_me"):
            continue
        message_id = str(last.get("id") or "")
        if not message_id:
            continue
        chat_state = state.get(chat_id) or {}
        if chat_state.get("last_replied_msg_id") == message_id:
            continue

        question = _short_chat_text(last.get("text") or "", limit=260)
        if not question:
            continue
        topic = topics.get(chat_id) or {}
        # B18: a rejection or «свяжемся при положительном решении» needs no answer.
        if topic.get("state") == "DISCARD" or _closing_kind(last.get("text") or ""):
            summary["skipped_closing"] += 1
            continue
        # B13: another profile of the same hh account owns this application.
        owner = _chat_owner(chat_id)
        if owner is not None and owner.status == "other":
            summary["skipped_other_profile"] += 1
            continue
        if owner is not None and owner.status == "own" and owner.direction != view.name:
            summary["skipped_other_direction"] += 1
            continue
        vacancy = dict(data.get("vacancy") or {})
        if topic.get("title") and (not vacancy.get("title") or _normalize_ai_marker_text(
                vacancy.get("title") or "") == "перейти"):
            vacancy["title"] = topic["title"]
        if topic.get("company") and vacancy.get("company") in (None, "", "—"):
            vacancy["company"] = topic["company"]
        preview_clean = _short_chat_text(preview_text, limit=180)
        if preview_clean and (not vacancy.get("title") or _normalize_ai_marker_text(vacancy.get("title") or "") == "перейти"):
            vacancy["title"] = preview_clean
        kind = _chat_candidate_kind(last)
        google_form_urls = extract_google_form_urls(last.get("text") or "", last.get("links") or [])
        if not google_form_urls:
            for msg in reversed(messages):
                if msg.get("is_me"):
                    continue
                google_form_urls = extract_google_form_urls(msg.get("text") or "", msg.get("links") or [])
                if google_form_urls:
                    break
        candidate = {
            "chat_id": chat_id,
            "message_id": message_id,
            "title": _short_chat_text(vacancy.get("title") or "—", limit=120),
            "company": _short_chat_text(vacancy.get("company") or "—", limit=80),
            "author": _short_chat_text(last.get("author") or "HR", limit=80),
            "question": question,
            "kind": kind,
            "kind_label": _chat_candidate_kind_label(kind),
            "allow_any": kind == "manual",
            "google_form_urls": google_form_urls[:3],
        }
        if owner is None or owner.status != "own" or not owner.resume_id:
            if len(summary["ownership_reviews"]) < max(1, limit):
                summary["ownership_reviews"].append(candidate)
        else:
            candidates.append(candidate)

    return summary


def _chat_result_caption(detail: dict, *, sent=False) -> str:
    vac = detail.get("vacancy") or {}
    title = html.escape((vac.get("title") or "—")[:160])
    company = html.escape((vac.get("company") or "—")[:160])
    question = html.escape((detail.get("question") or "")[:450])
    answer = html.escape(detail.get("answer") or "")
    chat_id = str(detail.get("chat_id") or "")
    heading = "Ответил в подозрительном HR-чате" if sent else "ИИ подготовил ответ"
    return (
        f"<b>{heading}</b>\n"
        f"{title} @ {company}\n\n"
        f"<i>{question}</i>\n\n"
        f"{answer}\n\n"
        f"{_human_chat_anchor(chat_id, detail.get('profile_name'))}"
    )


def _prepare_one_chat_preview(detail: dict) -> tuple:
    """Pure local rendering/validation before reserving notifier transport."""
    caption = _chat_result_caption(detail)
    if len(caption) > 4000:
        raise ValueError("Full approved chat preview exceeds Telegram message limit")
    raw_chat_id = str(detail.get("chat_id") or "")
    raw_message_id = str(detail.get("message_id") or "")
    markup = (
        build_chat_answer_preview_markup(
            detail.get("profile_name") or _active_profile_name(),
            raw_chat_id,
            raw_message_id,
            allow_any=bool(detail.get("manual_any")),
            draft_revision=detail.get("draft_revision", ""),
        )
        if raw_chat_id and raw_message_id
        else None
    )
    screenshot_path = (detail.get("preview") or {}).get("screenshot_path") or ""
    return caption, markup, screenshot_path


async def _notify_one_chat_result(notifier, detail: dict) -> bool:
    if notifier is None:
        return False
    if detail.get("sent"):
        return await notifier.send_message_with_markup(_chat_result_caption(detail, sent=True))
    caption, markup, screenshot_path = _prepare_one_chat_preview(detail)
    if screenshot_path and len(caption) <= 1000:
        return await notifier.send_photo(screenshot_path, caption=caption, reply_markup=markup)
    if screenshot_path:
        delivered = await notifier.send_photo(screenshot_path, caption="Preview ответа в HH")
        if delivered is not True:
            return False
    return await notifier.send_message_with_markup(caption, reply_markup=markup)


def _google_form_seen_key(form_url: str, message_id: str = "") -> str:
    return google_form_seen_key(form_url, message_id)


def _find_unseen_google_form_message(messages: list[dict], chat_state: dict) -> dict:
    seen = get_google_form_previews(chat_state)
    for msg in reversed(messages or []):
        if msg.get("is_me"):
            continue
        urls = extract_google_form_urls(msg.get("text") or "", msg.get("links") or [])
        for form_url in urls:
            message_id = str(msg.get("id") or "")
            key = _google_form_seen_key(form_url, message_id)
            previous = seen.get(key) or {}
            # Любой результат терминален для автоматического цикла.
            # Ошибки и формы, требующие ручного уточнения, повторяем только
            # по явной команде/кнопке, иначе один чат зациклит весь проход.
            if key not in seen:
                return {"message": msg, "form_url": form_url, "key": key}
    return {}


def _remember_google_form_preview(chat_state: dict, key: str, detail: dict) -> None:
    remember_google_form_preview(chat_state, key, detail)


async def _notify_google_form_failure(
    notifier,
    *,
    chat_id: str,
    vacancy: dict,
    message: dict,
    form_url: str,
    error: str,
    screenshot_path: str = "",
    runtime_paths: RuntimePaths | None = None,
) -> bool:
    if notifier is None:
        return False
    title = html.escape((vacancy.get("title") or "Google Form")[:160])
    company = html.escape((vacancy.get("company") or "—")[:160])
    question = html.escape((message.get("text") or "")[:500])
    safe_form_url = html.escape(form_url or "")
    safe_error = html.escape((error or "unknown error")[:300])
    caption = (
        "⚠️ <b>Google Form не удалось подготовить</b>\n"
        f"{title} @ {company}\n\n"
        f"<i>{question}</i>\n\n"
        f"Ошибка: {safe_error}\n"
        f"<a href='{safe_form_url}'>Открыть форму</a> · "
        f"{_human_chat_anchor(chat_id, runtime_paths=runtime_paths)}"
    )
    if screenshot_path and os.path.exists(screenshot_path):
        return await notifier.send_photo(screenshot_path, caption=caption)
    return await notifier.send_message_with_markup(caption)


async def _prepare_google_form_preview_from_message(
    page,
    *,
    chat_id: str,
    vacancy: dict,
    message: dict,
    form_url: str,
    notifier,
    runtime_paths: RuntimePaths | None = None,
    profile_name: str | None = None,
    ownership_binding=None,
) -> dict:
    import google_form_filler as gforms

    paths = runtime_paths or _runtime_paths()
    profile_name = profile_name or _active_profile_name()
    form_page = await page.context.new_page()
    try:
        detail = await gforms.preview_form(
            form_page,
            form_url,
            profile_name=profile_name,
            chat_id=chat_id,
            message_id=str(message.get("id") or ""),
            vacancy=vacancy,
            source_message=message.get("text") or "",
            notify=False,
            runtime_paths=paths,
            ownership_binding=ownership_binding,
        )
        if detail.get("ok") or detail.get("questions") or detail.get("status") == "already_submitted":
            if notifier:
                await gforms.notify_form_preview(detail, profile_name=profile_name)
        else:
            detail.setdefault("chat_id", chat_id)
            detail.setdefault("message_id", str(message.get("id") or ""))
            detail.setdefault("vacancy", vacancy)
            detail.setdefault("status", "preview_failed")
            await _notify_google_form_failure(
                notifier,
                chat_id=chat_id,
                vacancy=vacancy,
                message=message,
                form_url=detail.get("form_url") or form_url,
                error=detail.get("message") or "preview failed",
                screenshot_path=detail.get("screenshot_path") or "",
                runtime_paths=paths,
            )
    except HHUnexpectedUI:
        raise
    except Exception as exc:
        log.warning("google form preview failed for chat %s: %s", chat_id, type(exc).__name__)
        try:
            from private_artifacts import capture_artifacts
            saved = await capture_artifacts(form_page, paths.hh_state_dir, f"google_form_failed_{chat_id}",
                                            html=False, full_page=True)
            screenshot_path = saved.get("screenshot", "")
        except Exception as screenshot_exc:
            screenshot_path = ""
            log.warning("google form failure screenshot failed: %s", type(screenshot_exc).__name__)
        detail = {
            "ok": False,
            "message": f"{type(exc).__name__}: {exc}",
            "chat_id": chat_id,
            "message_id": str(message.get("id") or ""),
            "form_url": form_url,
            "vacancy": vacancy,
            "status": "preview_failed",
            "screenshot_path": screenshot_path,
        }
        await _notify_google_form_failure(
            notifier,
            chat_id=chat_id,
            vacancy=vacancy,
            message=message,
            form_url=form_url,
            error=detail["message"],
            screenshot_path=screenshot_path,
            runtime_paths=paths,
        )
    finally:
        try:
            await form_page.close()
        except Exception:
            pass
    return detail


def _message_revision(message: dict) -> tuple:
    return tuple(copy.deepcopy(message.get(field)) for field in
                 ("id", "text", "links", "author", "is_me", "is_ai", "is_ai_suspect"))


def _revision_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _candidate_revision(ownership_binding=None):
    from answer_grounding import current_candidate
    return _revision_hash([_active_profile_name(), asdict(current_candidate()),
                           asdict(ownership_binding) if ownership_binding else None])


def _live_candidate_revision(ownership_binding=None):
    from answer_grounding import capture_candidate
    from hh_client import _load_resume_text
    return _revision_hash([_active_profile_name(), asdict(capture_candidate(_load_resume_text())),
                           asdict(ownership_binding) if ownership_binding else None])


@analytics.best_effort
def _reply_audit_evidence(binding, snapshot):
    """Passive proof from the candidate and application checked before the click."""
    import chat_context
    owner = chat_context.safe_resolve(binding.home, binding.chat_id)
    return {
        "ownership_status": owner.status,
        "ownership_resume_id": owner.resume_id,
        "ownership_direction": owner.direction,
        "binding_resume_id": binding.resume_id,
        "binding_direction": binding.direction,
        "binding_profile_home": binding.home,
        "facts_profile_home": snapshot.profile_home,
        "facts_direction": snapshot.direction_name,
        "facts_digest": _revision_hash([snapshot.facts, snapshot.evidence_facts,
                                         snapshot.fact_constraints, snapshot.confirmed]),
        "resume_digest": hashlib.sha256(snapshot.resume.encode()).hexdigest(),
        "candidate_revision": _candidate_revision(binding),
        "analytics_run_id": str(analytics.current_context().get("run_id") or ""),
    }


@analytics.best_effort
def _record_reply_sent(chat_id, target_id, vacancy, answer, evidence):
    journal = active_chat_journal.get()
    if journal is not None:
        journal.write("chat_reply_sent", chat_id=chat_id, message_id=target_id,
                      sent=True, vacancy=vacancy, audit_evidence=evidence,
                      answer_digest=hashlib.sha256(answer.encode()).hexdigest(),
                      snapshot_ref=journal.latest_threads.get(chat_id, ""))


_reply_observation = ContextVar('chat_reply_observation', default=False)


def _observe_reply_attempt(operation):
    """Record one outcome, including early returns and exceptions, per operation."""
    signature = inspect.signature(operation)

    @wraps(operation)
    async def wrapped(*args, **kwargs):
        if _reply_observation.get():
            return await operation(*args, **kwargs)
        values = signature.bind(*args, **kwargs).arguments
        paths = values.get('runtime_paths') or values.get('paths') or _runtime_paths()
        owns_journal = active_chat_journal.get() is None
        journal = active_chat_journal.get() or ChatJournal(
            _state_repository(paths).path.parent, _active_profile_name())
        journal_token = active_chat_journal.set(journal)
        token = _reply_observation.set(True)
        detail = {'ok': False, 'reason': 'preparation_error'}
        try:
            detail = await operation(*args, **kwargs)
            detail.setdefault('reason', 'already_replied' if detail.get('already_replied') else
                              'notification_uncertain' if detail.get('notification_uncertain') else
                              'sent' if detail.get('sent') else 'drafted' if detail.get('ok') else
                              'preparation_error')
            return detail
        except asyncio.CancelledError:
            detail['reason'] = 'cancelled'
            raise
        except Exception as exc:
            detail['error_type'] = type(exc).__name__
            raise
        finally:
            try:
                chat_id = str(values.get('chat_id') or '')
                message_id = str(detail.get('message_id') or values.get('message_id') or
                                 (values.get('target') or {}).get('id') or '').partition('~')[0]
                log.info('chat_reply_attempt chat=%s msg=%s ok=%s reason=%s error_type=%s',
                         chat_id, message_id, bool(detail.get('ok')), detail['reason'],
                         detail.get('error_type', ''))
                journal.write('chat_reply_attempt', chat_id=chat_id, message_id=message_id,
                              kind='preview' if (values.get('dry_run') if values.get('dry_run') is not None
                                                 else _default_chat_dry_run()) else 'reply',
                              ok=bool(detail.get('ok')), reason=detail['reason'],
                              error_type=detail.get('error_type', ''),
                              answer=(detail.get('answer') or '')[:600],
                              snapshot_ref=journal.latest_threads.get(chat_id, ''))
            finally:
                _reply_observation.reset(token)
                active_chat_journal.reset(journal_token)
                if owns_journal:
                    journal.complete_audit()
    return wrapped


@_observe_reply_attempt
async def _execute_reply(
    repository, page, chat_id, target, messages, vacancy, resume_text, paths,
    *, dry_run, max_replies=None, repeat_preview=False, answer_kwargs=None, notify=None,
    draft_revision="", require_draft=False, notify_preflight=None,
    quote_min_similarity=1.0,
    ownership_binding=None,
) -> dict:
    """Claim before model/browser waits; persist only the owning chat's result."""
    chat_id, target_id = str(chat_id), str(target.get("id") or "")
    from answer_grounding import current_candidate
    if ownership_binding is None or ownership_binding.chat_id != chat_id:
        raise RuntimeError("Chat application ownership required")
    ownership_binding.check(current_candidate())
    target_revision = _message_revision(target)
    kind = "preview" if dry_run else "reply"
    # New replies without a stored human-approved draft must always be strict.
    quote_min_similarity = _quote_min_similarity(quote_min_similarity) if dry_run else 1.0
    candidate_revision = _candidate_revision(ownership_binding)
    source_revision = _revision_hash([target_revision, messages, vacancy])
    draft = repository.load().get(chat_id, {}).get("draft")
    stored = not dry_run and (bool(draft_revision) or (isinstance(draft, dict) and draft.get("message_id") == target_id) or require_draft)
    def matching_draft(value):
        return (isinstance(value, dict) and bool(draft_revision) and value.get("revision") == draft_revision
                and value.get("candidate_revision") == candidate_revision
                and value.get("source_revision") == source_revision
                and value.get("answer_hash") == hashlib.sha256(value.get("answer", "").encode()).hexdigest())
    if stored and not matching_draft(draft):
        return {"ok": False, "reason": "stale_draft", "stale": True, "message": "Approved draft missing or changed; create a new preview", "chat_id": chat_id}
    with repository.attempt(chat_id, kind, target_id, max_replies=max_replies,
                            repeat=dry_run and repeat_preview) as owner:
        if not owner:
            return {"ok": False, "reason": "attempt_blocked", "blocked": True, "message": "chat attempt blocked; inspect state", "chat_id": chat_id}
        if dry_run:
            # A new generation invalidates the previous revision immediately.
            repository.set_draft(chat_id, owner, None)
        diagnostics = {}
        if stored:
            # Claim excludes concurrent preview generation; re-read under that owner.
            draft = repository.load().get(chat_id, {}).get("draft")
            if not matching_draft(draft):
                return {"ok": False, "reason": "stale_draft", "stale": True, "chat_id": chat_id}
            # Reuse the already grounded, approved text. Revision/source/profile
            # checks below replace regeneration/re-audit; do not apply today's
            # threshold to a draft prepared under a different threshold.
            answer = draft["answer"]
        else:
            answer = await generate_answer(messages, vacancy, resume_text, diagnostics=diagnostics,
                                           quote_min_similarity=quote_min_similarity,
                                           **copy.deepcopy(answer_kwargs or {}))
        if not answer:
            return {"ok": False, "reason": diagnostics.get('reason', 'empty_answer'),
                    "message": "LLM did not produce answer", "chat_id": chat_id}
        # The model may have taken minutes. Never answer an obsolete question.
        fresh = await get_messages(page, chat_id)
        latest = (fresh.get("messages") or [{}])[-1]
        if _message_revision(latest) != target_revision or latest.get("is_me"):
            return {"ok": False, "reason": "stale_question", "stale": True, "message": "chat message changed", "chat_id": chat_id}
        detail = {"ok": True, "chat_id": chat_id, "message_id": target_id,
                  "vacancy": vacancy, "question": (target.get("text") or "")[:500],
                  "answer": answer, "dry_run": dry_run, "sent": False}
        if dry_run:
            ownership_binding.check(current_candidate())
            preview = await fill_and_preview(page, chat_id, answer, runtime_paths=paths)
            detail["preview"] = preview
            if not preview.get("filled"):
                detail.update(ok=False, reason="chat_unavailable", message="preview not filled")
                return detail
            def remember(chat):
                chat["last_previewed_msg_id"] = target_id
                chat["last_preview_answer_hash"] = hashlib.sha256(answer.encode()).hexdigest()[:16]
                chat["last_previewed_at"] = time.time()
            draft = {"message_id": target_id, "answer": answer, "answer_hash": hashlib.sha256(answer.encode()).hexdigest(),
                     "revision": uuid.uuid4().hex[:12], "candidate_revision": candidate_revision,
                     "source_revision": source_revision, "quote_min_similarity": quote_min_similarity}
            repository.set_draft(chat_id, owner, draft)
            detail["draft_revision"] = draft["revision"]
            # Reserve delivery before await. Uncertain notifications are not replayed.
            if notify is not None:
                if notify_preflight is not None:
                    try:
                        notify_preflight(detail)
                    except ValueError as exc:
                        repository.set_draft(chat_id, owner, None)
                        detail.pop("draft_revision", None)
                        detail.update(ok=False, reason="preview_too_long", notification_rejected=True, message=str(exc))
                        if not repository.finish(chat_id, owner, "failed"):
                            raise RuntimeError("Chat preview rejection ownership lost")
                        return detail
                repository.mark_acting(chat_id, owner)
                try:
                    if await notify(detail) is False:
                        raise RuntimeError("Preview delivery not acknowledged")
                except Exception as exc:
                    log.warning("chat preview notification failed: %s", type(exc).__name__)
                    detail["notification_uncertain"] = True
                    repository.finish(chat_id, owner, "uncertain", remember)
                    return detail
            if not repository.finish(chat_id, owner, "completed", remember):
                raise RuntimeError("Chat preview completion ownership lost")
        else:
            audit_evidence = None
            async def before_send():
                nonlocal audit_evidence
                data = await _extract_messages(page)
                latest = (data.get("messages") or [{}])[-1]
                if _message_revision(latest) != target_revision or latest.get("is_me"):
                    raise RuntimeError("Chat changed before send")
                if stored:
                    if (not matching_draft(repository.load().get(chat_id, {}).get("draft"))
                            or _live_candidate_revision(ownership_binding) != candidate_revision):
                        raise RuntimeError("Approved chat candidate/draft changed before send")
                ownership_binding.check(current_candidate())
                audit_evidence = _reply_audit_evidence(ownership_binding, current_candidate())
                # Last synchronous durable check immediately before the click.
                repository.mark_acting(chat_id, owner)
            ok = await send_message(page, chat_id, answer, runtime_paths=paths, before_send=before_send,
                                    on_no_action=lambda: repository.confirm_no_action(chat_id, owner))
            detail["sent"] = ok
            if not ok:
                detail.update(ok=False, reason="send_uncertain", message="send failed or uncertain; inspect state")
                return detail
            _record_reply_sent(chat_id, target_id, vacancy, answer, audit_evidence)
            if not repository.finish(chat_id, owner, "completed"):
                raise RuntimeError("Chat send completion ownership lost")
            if notify is not None:
                try:
                    await notify(detail)
                except Exception as exc:
                    log.warning("chat sent notification failed: %s", type(exc).__name__)
        return detail


from answer_grounding import candidate_operation


@_observe_reply_attempt
@candidate_operation(client_factory=lambda: _get_llm_client())
@analytics.chat_context
async def process_one(
    hh_client,
    chat_id: str,
    *,
    message_id: str = "",
    allow_suspicious: bool = False,
    allow_any: bool = False,
    alternative: bool = False,
    dry_run: bool | None = None,
    notify: bool = False,
    runtime_paths: RuntimePaths | None = None,
) -> dict:
    """Generate/send one reply for a specific chat message after human approval."""
    paths = runtime_paths or _runtime_paths()
    profile_name = _active_profile_name()
    message_id, separator, draft_revision = str(message_id).partition("~")
    if separator and not re.fullmatch(r"[0-9a-f]{12}", draft_revision):
        return {"ok": False, "reason": "stale_draft", "message": "Invalid draft revision", "chat_id": chat_id}
    if dry_run is None:
        dry_run = _default_chat_dry_run()
    from answer_grounding import current_candidate
    from chat_context import bind_candidate
    ownership_binding, refusal = await bind_candidate(
        chat_id, current_candidate(), home=paths.home_dir, profile_name=profile_name)
    if refusal:
        if notify and refusal["reason"] == "unknown_owner":
            import notifier
            await _notify_ownership_review(_state_repository(paths), notifier, refusal,
                                           message_id=message_id, profile_name=profile_name)
        return refusal

    if not hh_client._page:
        await hh_client.start(headless=True)
    page = hh_client._page
    await page.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1500)

    data = await get_messages(page, chat_id)
    messages = data.get("messages", [])
    target = _find_message(messages, message_id)
    if not target:
        return {"ok": False, "reason": "missing_question", "message": "message not found", "chat_id": chat_id}
    if target.get("is_me"):
        return {"ok": False, "reason": "stale_question", "message": "target message is ours", "chat_id": chat_id}

    target_id = str(target.get("id") or "")
    if not target_id or _message_revision(target) != _message_revision(messages[-1]):
        return {"ok": False, "reason": "stale_question", "message": "target message is no longer latest", "chat_id": chat_id}
    repository = _state_repository(paths)
    chat_state = repository.load().get(str(chat_id), {})
    if chat_state.get("last_replied_msg_id") == target_id:
        return {"ok": True, "already_replied": True, "message": "already replied", "chat_id": chat_id}

    is_regular_ai = bool(target.get("is_ai"))
    is_approved_suspicious = allow_suspicious and bool(target.get("is_ai_suspect"))
    is_manual_any = bool(allow_any) and not bool(target.get("is_me"))
    if not is_regular_ai and not is_approved_suspicious and not is_manual_any:
        return {
            "ok": False,
            "reason": "question_not_approved",
            "message": "target message is not AI/suspicious/manual-approved screening",
            "chat_id": chat_id,
            "question": (target.get("text") or "")[:300],
        }

    try:
        from hh_client import _load_resume_text
    except Exception:
        _load_resume_text = lambda: ""
    from answer_grounding import current_candidate
    resume_text = current_candidate().resume
    vacancy = data.get("vacancy", {})
    def prepare_notification(detail):
        detail.update(suspicious=is_approved_suspicious, manual_any=is_manual_any,
                      profile_name=profile_name)
        return _prepare_one_chat_preview(detail)

    async def notify_result(detail):
        detail.update(suspicious=is_approved_suspicious, manual_any=is_manual_any,
                      profile_name=profile_name)
        if notify:
            try:
                import notifier
            except Exception:
                notifier = None
            return await _notify_one_chat_result(notifier, detail)

    detail = await _execute_reply(
        repository, page, str(chat_id), copy.deepcopy(target), copy.deepcopy(messages),
        copy.deepcopy(vacancy), resume_text, paths, dry_run=dry_run,
        repeat_preview=True, notify=notify_result if notify else None,
        notify_preflight=prepare_notification if notify else None,
        draft_revision=draft_revision, require_draft=bool(separator),
        # Only this preview flow exposes Send / Another variant for approval.
        # The automatic scan keeps the default strict policy, even in dry-run.
        quote_min_similarity=_quote_min_similarity(os.getenv("CHAT_DRAFT_QUOTE_MIN_SIMILARITY", "0.85"))
        if dry_run else 1.0,
        ownership_binding=ownership_binding,
        answer_kwargs={
            "question_message": target if (is_approved_suspicious or is_manual_any) else None,
            "question_kind": (
                "подозрительное HR-сообщение"
                if is_approved_suspicious
                else "ручное HR-сообщение"
                if is_manual_any
                else "AI-помощник"
            ),
            "alternative": alternative,
        },
    )
    detail.update(suspicious=is_approved_suspicious, manual_any=is_manual_any,
                  profile_name=profile_name)
    return detail


# ── Main process loop ──────────────────────────────────────────────────────

@candidate_operation(client_factory=lambda: _get_llm_client())
@analytics.chat_context
async def process_all(
    hh_client,
    dry_run: bool | None = None,
    max_replies_per_chat: int | None = None,
    *,
    limits: ChatResponderLimits | None = None,
    runtime_paths: RuntimePaths | None = None,
) -> dict:
    """Main entry: polling + reply.

    dry_run: если None — берётся из env HH_CHAT_AUTOSEND (0 = dry-run).
    """
    if dry_run is None:
        dry_run = _default_chat_dry_run()
    paths = runtime_paths or _runtime_paths()
    runtime_limits = limits or ChatResponderLimits.from_env(os.environ)
    max_replies = max_replies_per_chat or runtime_limits.max_replies_per_chat
    profile_name = _active_profile_name()
    repository = _state_repository(paths)

    if not hh_client._page:
        await hh_client.start(headless=True)
    page = hh_client._page

    # Загрузить cookies через домашнюю страницу hh.ru
    await page.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1500)

    repository.load()  # Fail closed before running model/notification workflows.
    summary = {
        "chats_scanned": 0,
        "with_ai": 0,
        "suspicious": 0,
        "suspicious_notified": 0,
        "google_forms_found": 0,
        "google_forms_prepared": 0,
        "google_forms_failed": 0,
        "answers_drafted": 0,
        "answers_sent": 0,
        "skipped": 0,
        "read_failures": 0,
        "details": [],
    }

    journal = ChatJournal(repository.path.parent, profile_name)
    summary.update(run_id=journal.run_id, decisions={}, hr_questions=0, hr_notified=0,
                   external_invites=0, external_notified=0, rejections=[], waiting=[])
    topics = _latest_topics()
    chats = await list_chats(page)
    list_ref = journal.write(
        "chat_list_snapshot", list_complete=bool(getattr(chats, "list_complete", False)),
        stop_reason=getattr(chats, "stop_reason", "unknown"),
        rows=[dict(chat_id=str(row.get("chat_id") or ""), position=i,
                   raw_text=row.get("raw_text", row.get("preview") or ""),
                   preview=row.get("preview") or "", unread=bool(row.get("unread")),
                   unread_evidence=row.get("unread_evidence") or "")
              for i, row in enumerate(chats)],
    )

    decided_chat_ids = set()

    def record_decision(chat_id, message_id, decision, reason, snapshot_ref):
        snapshot_ref = journal.latest_threads.get(chat_id, snapshot_ref)
        journal.write("chat_decision", chat_id=chat_id, message_id=message_id,
                      decision=decision, reason=reason, snapshot_ref=snapshot_ref)
        decided_chat_ids.add(chat_id)
        counts = summary["decisions"]
        counts[decision] = counts.get(decision, 0) + 1
        log.info("chat decision run=%s chat=%s message=%s decision=%s reason=%s snapshot=%s",
                 journal.run_id, chat_id, message_id, decision, reason, snapshot_ref)

    eligible_chats = []
    for chat in chats:
        chat_id = str(chat.get("chat_id") or "")
        preview = chat.get("preview") or ""
        if not chat_id or _is_application_only_preview(preview) or _is_blocked_company_preview(preview):
            reason = ("missing_chat_id" if not chat_id else "application_only_preview"
                      if _is_application_only_preview(preview) else "blocked_company_preview")
            record_decision(chat_id, "", "filtered", reason, list_ref)
        else:
            eligible_chats.append(chat)
    scan_chats = select_chat_batch(journal.home, eligible_chats, runtime_limits.max_scan)
    selected_ids = {str(chat["chat_id"]) for chat in scan_chats}
    for chat in eligible_chats:
        if str(chat["chat_id"]) not in selected_ids:
            record_decision(str(chat["chat_id"]), "", "out_of_batch", "scan_budget", list_ref)
    summary.update(chats_total=len(chats), chats_eligible=len(eligible_chats),
                   chats_scanned=len(scan_chats), chats_read=0, max_scan=runtime_limits.max_scan)
    log.info("found %d chats total, %d eligible; scanning rotating batch %d (limit=%d)",
             len(chats), len(eligible_chats), len(scan_chats), runtime_limits.max_scan)

    from answer_grounding import current_candidate
    resume_text = current_candidate().resume
    try:
        import notifier
    except Exception:
        notifier = None
    diagnostic_budget = max(0, int(os.environ.get("HH_CHAT_DIAGNOSTIC_SNAPSHOTS", "0")))

    journal_token = active_chat_journal.set(journal)
    try:
        for chat in scan_chats:
            chat_id = str(chat["chat_id"])
            decision, reason, message_id, snapshot_ref = "unknown", "processing_interrupted", "", list_ref
            try:
                chat_state = repository.load().get(chat_id, {})
                try:
                    data = await get_messages(page, chat_id)
                    summary["chats_read"] += 1
                except HHUnexpectedUI:
                    decision, reason = "read_failed", "ui_stop"
                    raise
                except Exception as exc:
                    decision, reason = "read_failed", type(exc).__name__
                    summary["read_failures"] += 1
                    continue
                msgs = data.get("messages", [])
                snapshot_ref = data.get("_journal_snapshot_id") or journal.thread(chat_id, data)
                import hiring_research
                from chat_context import safe_resolve
                hiring_research.record_screening(data.get("vacancy") or {}, msgs,
                                                 safe_resolve(paths.home_dir, chat_id))
                vac = dict(data.get("vacancy", {}) or {})

                # Resolve the application resume before chat replies or linked forms.
                form_item = _find_unseen_google_form_message(msgs, chat_state)
                decision, reason, last = decide_chat(msgs, chat_state, dry_run=dry_run, max_replies=max_replies)
                message_id = str(((form_item or {}).get("message") or last or {}).get("id") or "")
                ownership_binding = None
                if form_item or decision in {"hr_question", "external_invite", "ai_hh_autoreply"}:
                    from chat_context import bind_candidate
                    ownership_binding, refusal = await bind_candidate(
                        chat_id, current_candidate(), home=paths.home_dir, profile_name=profile_name)
                    if refusal:
                        decision = reason = refusal["reason"]
                        summary["skipped"] += 1
                        if reason == "unknown_owner":
                            await _notify_ownership_review(repository, notifier, refusal, message_id=message_id,
                                                           profile_name=profile_name, vacancy=vac)
                        summary["details"].append(refusal)
                        continue

                # Older incoming form links keep precedence, after the ownership gate.
                if form_item:
                    form_msg, form_url = form_item["message"], form_item["form_url"]
                    message_id = str(form_msg.get("id") or "")
                    decision, reason = "google_form", "existing_form_preview"
                    preview_text = (chat.get("preview") or "").strip()
                    if preview_text and (not vac.get("title") or _normalize_ai_marker_text(vac.get("title") or "") == "перейти"):
                        vac["title"] = preview_text
                    summary["google_forms_found"] += 1
                    with repository.attempt(chat_id, "form", form_item["key"]) as owner:
                        if not owner:
                            decision, reason = "already_handled", "form_claim_unavailable"
                            summary["skipped"] += 1
                            continue
                        repository.mark_acting(chat_id, owner)
                        detail = await _prepare_google_form_preview_from_message(
                            page, chat_id=chat_id, vacancy=copy.deepcopy(vac),
                            message=copy.deepcopy(form_msg), form_url=form_url,
                            notifier=notifier, runtime_paths=paths, profile_name=profile_name,
                            ownership_binding=ownership_binding,
                        )
                        if not repository.finish(chat_id, owner, "completed", lambda current:
                                _remember_google_form_preview(current, form_item["key"], detail)):
                            raise RuntimeError("Chat form completion ownership lost")
                    summary["google_forms_prepared" if detail.get("ok") else "google_forms_failed"] += 1
                    reason = "form_prepared" if detail.get("ok") else "form_preview_failed"
                    summary["details"].append(dict(chat_id=chat_id, google_form=True, ok=bool(detail.get("ok"))))
                    continue

                if decision == "hr_question" or (decision == "ours_last" and any(
                    not msg.get("is_me") and question_reason(msg) != "ambiguous_incoming_requires_review"
                    for msg in msgs[:-1]
                )):
                    try:
                        await diagnostic_snapshot(journal, page, chat_id, message_id,
                                                  decision, snapshot_ref, diagnostic_budget)
                    except Exception as exc:
                        log.warning("chat diagnostic failed chat=%s error=%s", chat_id, type(exc).__name__)

                if decision in {"hr_question", "external_invite"}:
                    suspicious = bool(last.get("is_ai_suspect")) and decision == "hr_question"
                    summary["hr_questions" if decision == "hr_question" else "external_invites"] += 1
                    if suspicious:
                        summary["suspicious"] += 1
                    if (chat_state.get("last_notice_msg_id") == message_id
                            or chat_state.get("last_suspicious_msg_id") == message_id):
                        decision, reason = "already_handled", "notice_already_delivered"
                        summary["skipped"] += 1
                        continue
                    notified = False
                    rejected_chat = False
                    with repository.attempt(chat_id, "notice", message_id) as owner:
                        if not owner:
                            decision, reason = "already_handled", "notice_claim_unavailable"
                            summary["skipped"] += 1
                            continue
                        if notifier:
                            repository.mark_acting(chat_id, owner)
                            try:
                                topic = topics.get(chat_id) or {}
                                # The chat header often yields «Перейти @ —»; the snapshot knows better.
                                if topic.get("title") and (not vac.get("title") or _normalize_ai_marker_text(
                                        vac.get("title") or "") == "перейти"):
                                    vac["title"] = topic["title"]
                                if topic.get("company") and vac.get("company") in (None, "", "—"):
                                    vac["company"] = topic["company"]
                                closing = ("rejection" if topic.get("state") == "DISCARD"
                                           else _closing_kind(last.get("text") or ""))
                                rejected_chat = decision == "hr_question" and bool(closing)
                                if rejected_chat:
                                    # The employer closed the exchange; usually the chat is closed too.
                                    # One digest after the scan instead of a reply card.
                                    summary["rejections" if closing == "rejection" else "waiting"].append(
                                        dict(title=vac.get("title") or "—", company=vac.get("company") or "—"))
                                    notified = True
                                elif suspicious:
                                    notified = await notify_suspicious_screening_message(
                                        notifier, chat_id=chat_id, vacancy=vac, message=last, profile_name=profile_name)
                                else:
                                    notified = await notify_hr_message(
                                        notifier, chat_id=chat_id, vacancy=vac, message=last, profile_name=profile_name,
                                        invitation=external_invitation(last) if decision == "external_invite" else None)
                            except Exception as exc:
                                log.warning("chat notice failed chat=%s error=%s", chat_id, type(exc).__name__)
                        if notified:
                            def remember_notice(current):
                                current["last_notice_msg_id"] = message_id
                                current["last_notice_kind"] = decision
                                if suspicious:
                                    current["last_suspicious_msg_id"] = message_id
                                    current["last_suspicious_at"] = time.time()
                            if not repository.finish(chat_id, owner, "completed", remember_notice):
                                raise RuntimeError("Chat notification completion ownership lost")
                    reason += ":notice_delivered" if notified else ":notice_unavailable_or_uncertain"
                    if notified and not rejected_chat:
                        summary["hr_notified" if decision == "hr_question" else "external_notified"] += 1
                        if suspicious:
                            summary["suspicious_notified"] += 1
                    summary["details"].append(dict(chat_id=chat_id, decision=decision, notified=bool(notified)))
                    continue
                if decision != "ai_hh_autoreply":
                    summary["skipped"] += 1
                    continue
                summary["with_ai"] += 1

                async def notify_reply(detail):
                    if notifier is None:
                        return
                    if detail["dry_run"]:
                        preview = detail.get("preview") or {}
                        caption = _dry_run_caption(vac, last, detail["answer"])
                        if preview.get("screenshot_path"):
                            return await notifier.send_photo(preview["screenshot_path"], caption=caption)
                        return await notifier.send_message_with_markup(caption)
                    return await notifier.send_message_with_markup(_sent_caption(chat_id, vac, last, detail["answer"], runtime_paths=paths))

                detail = await _execute_reply(
                    repository, page, chat_id, copy.deepcopy(last), copy.deepcopy(msgs),
                    copy.deepcopy(vac), resume_text, paths, dry_run=dry_run, max_replies=max_replies,
                    notify=notify_reply if notifier is not None else None,
                    ownership_binding=ownership_binding,
                )
                if not detail.get("answer"):
                    reason = "stale_question" if detail.get("stale") else "attempt_blocked" if detail.get("blocked") else "no_grounded_answer"
                    summary["skipped"] += 1
                    continue
                summary["answers_drafted"] += 1
                reason = "sent" if detail.get("sent") else "drafted" if detail.get("ok") else "send_or_preview_failed"
                if detail.get("sent"):
                    summary["answers_sent"] += 1
                elif not detail.get("ok"):
                    summary["skipped"] += 1
                summary["details"].append(dict(chat_id=chat_id, decision=decision, ok=bool(detail.get("ok")),
                                                dry_run=bool(detail.get("dry_run")), sent=bool(detail.get("sent"))))
                await asyncio.sleep(runtime_limits.reply_cooldown_s)
            except BaseException as exc:
                if reason != "ui_stop":
                    reason = "processing_interrupted:" + type(exc).__name__
                raise
            finally:
                record_decision(chat_id, message_id, decision, reason, snapshot_ref)

    finally:
        active_chat_journal.reset(journal_token)
        try:
            for chat in scan_chats:
                chat_id = str(chat["chat_id"])
                if chat_id not in decided_chat_ids:
                    record_decision(chat_id, "", "out_of_batch", "run_stopped_before_open", list_ref)
        finally:
            journal.complete_audit()

    log.info("chat scan complete: read=%d/%d, failures=%d",
             summary["chats_read"], summary["chats_scanned"], summary["read_failures"])
    if summary["rejections"] or summary["waiting"]:
        try:
            await _notify_rejections(_notifier_module(), summary["rejections"], summary["waiting"])
        except Exception as exc:
            log.warning("rejection digest failed: %s", type(exc).__name__)
    return summary
