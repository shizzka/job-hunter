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
import html
import logging
import os
import re
import time
from typing import Any

import config
from llm_client import get_llm_client
from llm_utils import parse_llm_json
from google_form_filler import extract_google_form_urls
from hh.chat import (
    CHATIK_CHAT_READY_SELECTOR,
    CHATIK_NAVIGATION_ATTEMPTS,
    CHATIK_NAVIGATION_TIMEOUT_MS,
    CHATIK_READY_TIMEOUT_MS,
    CHATIK_ROOT,
    message_matches_sent_text as _chat_message_matches_sent_text,
    messages_contain_sent_text as _chat_messages_contain_sent_text,
    normalize_sent_message_text as _chat_normalize_sent_message_text,
    open_chatik_page as _chat_open_chatik_page,
    quick_reply_choice as _chat_quick_reply_choice,
    reset_page_after_navigation_failure as _chat_reset_page_after_navigation_failure,
)
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



def _is_application_only_preview(preview: str) -> bool:
    """HH lists fresh applications as chats before a real dialog exists."""
    return _normalize_ai_marker_text(preview).endswith("отклик на вакансию")


def _is_blocked_company_preview(preview: str) -> bool:
    """HH can list blocked employers as chats while the direct chat page never renders."""
    return "компания заблокирована" in _normalize_ai_marker_text(preview)


# ── State ───────────────────────────────────────────────────────────────────

def _state_repository() -> ChatResponderStateRepository:
    home = os.path.dirname(config.RESUME_FILE) or os.path.expanduser("~/.job-hunter")
    return ChatResponderStateRepository(home, logger=log)


def _state_path() -> str:
    return str(_state_repository().path)


def load_state() -> dict:
    return _state_repository().load()


def save_state(state: dict) -> None:
    _state_repository().save(state)


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
    """Открыть chatik root и вернуть свежие чаты с metadata.

    Chatik виртуализует список: в DOM присутствует только видимое окно.
    Поэтому нельзя сначала проскроллить вниз, а потом читать DOM — так
    теряются свежие чаты из верхнего окна.
    """
    await _open_chatik_page(
        page,
        f"{CHATIK_ROOT}/",
        '[data-qa^="chatik-open-chat-"]',
        settle_ms=2000,
    )

    chats = await page.evaluate("""async () => {
        const all = [...document.querySelectorAll('*')];
        const scroller = all.filter(el => {
            const cs = getComputedStyle(el);
            return (cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                && el.scrollHeight > el.clientHeight + 50;
        }).sort((a,b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];
        const seen = new Map();
        const collect = () => {
            const items = [...document.querySelectorAll('[data-qa^="chatik-open-chat-"]')];
            for (const el of items) {
                const qa = el.getAttribute('data-qa') || '';
                const idMatch = qa.match(/^chatik-open-chat-(\\d+)$/);
                if (!idMatch) continue;
                const id = idMatch[1];
                if (seen.has(id)) continue;
                const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                seen.set(id, {chat_id: id, preview: text.slice(0, 300)});
            }
        };

        collect();
        if (scroller) {
            scroller.scrollTop = 0;
            await new Promise(r => setTimeout(r, 500));
            collect();
            let last = -1;
            // Проверяем свежую верхнюю часть списка. Пустые записи новых откликов
            // позже отфильтруются без открытия страницы чата.
            for (let i = 0; i < 10; i++) {
                scroller.scrollTop += Math.max(320, Math.floor(scroller.clientHeight * 0.75));
                await new Promise(r => setTimeout(r, 250));
                collect();
                if (scroller.scrollTop === last) break;
                last = scroller.scrollTop;
            }
        }
        return [...seen.values()];
    }""")
    return chats


# ── Message extraction ──────────────────────────────────────────────────────

async def _extract_messages(page) -> dict[str, Any]:
    """Read messages from the currently open chat without navigating."""
    data = await page.evaluate("""() => {
        // основные bubbles
        const bubbles = [...document.querySelectorAll('[data-qa^="chatik-chat-message-"]')]
            .filter(el => /^chatik-chat-message-\\d+$/.test(el.getAttribute('data-qa') || ''));
        const out = [];
        for (const b of bubbles) {
            const qa = b.getAttribute('data-qa');
            const idMatch = qa.match(/^chatik-chat-message-(\\d+)$/);
            if (!idMatch) continue;
            const mid = idMatch[1];
            // text
            const textEl = b.querySelector('[data-qa$="-text"]')
                       || b.querySelector('[data-qa="chatik-chat-message-' + mid + '-text"]');
            const text = textEl ? (textEl.innerText || '').trim() : '';
            // author label inside the bubble
            const authorEl = b.querySelector('[data-qa="chat-bubble-author-name"]');
            const author = authorEl ? authorEl.innerText.trim() : '';
            // Avatar can be an img or an icon with aria-label (robot recruiter).
            const avatarImg = b.querySelector('img[alt]');
            const avatarLabelEl = b.querySelector(
                '[data-qa="chat-bubble-wrapper"] [aria-label]'
            );
            const avatarAlt = avatarImg
                ? (avatarImg.getAttribute('alt') || '')
                : (avatarLabelEl?.getAttribute('aria-label') || '');
            const avatarSrc = avatarImg ? avatarImg.src : '';
            // Incoming continuation bubbles may omit author/avatar. Outgoing CSS
            // markers are the reliable way to identify our messages.
            const is_me = Boolean(
                b.querySelector('[class*="chat-bubble_outgoing"]')
                || b.querySelector('[class*="message_my"]')
            );
            const links = [...b.querySelectorAll('a[href]')].map(a => ({
                href: a.href || '',
                text: (a.innerText || '').trim(),
            }));
            out.push({
                id: mid,
                text,
                author,
                avatar_alt: avatarAlt,
                avatar_src: avatarSrc,
                links,
                is_ai: false,
                is_me,
                is_other: !is_me,
            });
        }
        // page title / chat header — для extract названия вакансии и компании
        const headerCompany = (document.querySelector('[data-qa="chat-header-title"]')
                            || document.querySelector('header h1, h1'))?.innerText || '';
        const vacancyLink = document.querySelector('a[href*="/vacancy/"]');
        const vacancy = {
            title: vacancyLink ? vacancyLink.innerText.trim() : '',
            url: vacancyLink ? vacancyLink.href : '',
            company: headerCompany.trim(),
        };
        return {messages: out, vacancy};
    }""")
    for message in data.get("messages", []):
        _classify_message_author(message)
    return data


async def get_messages(page, chat_id: str) -> dict[str, Any]:
    """Открыть chat прямой URL, вернуть messages+vacancy."""
    url = f"{CHATIK_ROOT}/chat/{chat_id}"
    await _open_chatik_page(
        page,
        url,
        CHATIK_CHAT_READY_SELECTOR,
        settle_ms=2500,
    )
    return await _extract_messages(page)


async def get_messages_safe(
    page,
    chat_id: str,
    *,
    attempts: int = 1,
    ready_timeout_ms: int = 5000,
    settle_ms: int = 800,
) -> dict[str, Any]:
    """Best-effort chat read for scanners that should skip broken/empty chats quickly."""
    url = f"{CHATIK_ROOT}/chat/{chat_id}"
    try:
        await _open_chatik_page(
            page,
            url,
            CHATIK_CHAT_READY_SELECTOR,
            settle_ms=settle_ms,
            attempts=attempts,
            ready_timeout_ms=ready_timeout_ms,
            log_failures=False,
        )
        data = await _extract_messages(page)
        data.setdefault("error", "")
        return data
    except Exception as exc:
        await _reset_page_after_navigation_failure(page)
        return {
            "messages": [],
            "vacancy": {},
            "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:180]}",
            "chat_id": str(chat_id),
        }


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
) -> str | None:
    """Сгенерировать ответ на вопрос AI-помощника или approved suspicious HR message."""
    if not messages:
        return None
    target_message = question_message or next((m for m in reversed(messages) if m.get("is_ai")), None)
    if not target_message:
        return None
    question = target_message.get("text", "").strip()
    if not question:
        return None
    deterministic_answer = _deterministic_chat_answer(question)
    if deterministic_answer:
        return deterministic_answer

    # Контексты
    from prompt_blocks import (
        build_profile_note_block,
        build_facts_block,
        build_salary_rule_block,
        build_knowledge_base_block,
        build_filtered_kb_block,
    )
    profile_note = build_profile_note_block()
    facts = build_facts_block()
    salary = build_salary_rule_block()
    # 2-pass: фильтруем KB под вакансию (используем title+company как контекст)
    vacancy_summary = f"Должность: {vacancy.get('title','')}\nКомпания: {vacancy.get('company','')}\n"
    try:
        knowledge = await build_filtered_kb_block(
            vacancy_summary, _get_llm_client(), max_sections=5, limit_chars=8000,
        )
    except Exception as exc:
        log.warning("filtered KB selection failed, fallback to full: %s", exc)
        knowledge = build_knowledge_base_block(limit_chars=8000)

    vacancy_block = ""
    if vacancy.get("title") or vacancy.get("company"):
        vacancy_block = (
            f"Контекст вакансии:\n"
            f"- Должность: {vacancy.get('title', '—')}\n"
            f"- Компания: {vacancy.get('company', '—')}\n\n"
        )

    dialog_block = _format_dialog(messages, take_last=10)

    prompt = f"""Ты отвечаешь в чате hh.ru от лица кандидата на вопрос работодателя.

Тип вопроса: {question_kind}. Если это похоже на автоматический HR-скрининг, отвечай так же конкретно, как AI-помощнику, но без упоминания, что собеседник является ботом. На вопрос о зарплатных ожиданиях отвечай по блоку зарплатных ожиданий ниже.

Опирайся на канонический профиль, структурированные факты, резюме и контекст вакансии. Если факт отсутствует — отвечай ЧЕСТНО: «нет такого опыта», «не работал с этим», «изучаю сейчас». Не выдумывай инструменты/языки/опыт.

Если AI спрашивает количество лет опыта в конкретной технологии — назови конкретно или скажи «не работал». QA-опыт кандидата — около 1 года практического тестирования, не путай с общим инженерным.

Длина ответа: 2-4 предложения, конкретика. Без приветствий («Здравствуйте» — не нужно, мы уже в диалоге). Без шаблонных оборотов «активно», «успешно», «эффективно», «глубокий опыт».

{profile_note}{knowledge}{facts}{salary}{vacancy_block}История диалога (последние 10 реплик):
{dialog_block}

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
- Отвечай как Eugene в первом лице."""

    model = (getattr(config, "HH_CHAT_RESPONDER_MODEL", "") or "").strip() or config.LLM_MODEL
    try:
        client = _get_llm_client()
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Ты отвечаешь строго JSON. Никакого текста до или после JSON-объекта."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=600,
        )
        raw = (resp.choices[0].message.content or "").strip()
        parsed = parse_llm_json(raw)
    except Exception as exc:
        log.warning("LLM chat-answer failed: %s", exc)
        return None

    if parsed.get("status") != "answer":
        return None
    answer = str(parsed.get("answer", "")).strip()
    if answer and _looks_like_screening_form_artifact(answer) and not _is_screening_form_question(question):
        log.warning(
            "LLM chat-answer dropped form artifact for non-form question: %s",
            question[:160],
        )
        return None
    return answer or None


# ── Send ────────────────────────────────────────────────────────────────────

async def _dismiss_cookies_banner(page) -> None:
    """Закрыть баннер «Мы используем файлы cookie» если есть — чтобы не перекрывал скрин."""
    for sel in (
        '[data-qa="cookies-policy-informer-accept"]',
        '[data-qa="cookies-policy-banner-accept"]',
        'button:has-text("Понятно")',
    ):
        try:
            btn = await page.query_selector(sel)
        except Exception:
            btn = None
        if btn:
            try:
                await btn.click()
                await page.wait_for_timeout(400)
                return
            except Exception:
                continue


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
    if not choice:
        return None
    for button in await page.query_selector_all("button"):
        try:
            if (await button.inner_text()).strip() != choice:
                continue
            if await button.is_visible() and await button.is_enabled():
                return button
        except Exception:
            continue
    return None


async def fill_and_preview(page, chat_id: str, text: str) -> dict:
    """Перейти в chat, набрать текст в input. НЕ отправлять.
    Возвращает {filled, screenshot_path}."""
    url = f"{CHATIK_ROOT}/chat/{chat_id}"
    await _open_chatik_page(
        page,
        url,
        CHATIK_CHAT_READY_SELECTOR,
        settle_ms=2000,
    )
    await _dismiss_cookies_banner(page)
    quick_reply = _quick_reply_choice(text)
    quick_button = await _find_quick_reply_button(page, quick_reply)
    if quick_button:
        shot_path = os.path.join(config.HH_STATE_DIR, f"chat_preview_{chat_id}_{int(time.time())}.png")
        try:
            await page.screenshot(path=shot_path)
        except Exception:
            shot_path = ""
        return {
            "filled": True,
            "quick_reply": quick_reply,
            "screenshot_path": shot_path,
        }

    inp = await page.query_selector('textarea[data-qa="chatik-new-message-text"]')
    if not inp:
        return {"filled": False, "reason": "input not found"}
    await inp.focus()
    await inp.fill(text)
    await page.wait_for_timeout(500)
    shot_path = os.path.join(config.HH_STATE_DIR, f"chat_preview_{chat_id}_{int(time.time())}.png")
    try:
        await page.screenshot(path=shot_path)
    except Exception:
        shot_path = ""
    return {"filled": True, "screenshot_path": shot_path}


async def send_message(page, chat_id: str, text: str) -> bool:
    """Полная отправка: перейти, набрать, нажать Send."""
    result = await fill_and_preview(page, chat_id, text)
    if not result.get("filled"):
        return False
    quick_reply = result.get("quick_reply") or ""
    btn = (
        await _find_quick_reply_button(page, quick_reply)
        if quick_reply
        else await page.query_selector('[data-qa="chatik-do-send-message"]')
    )
    if not btn:
        log.warning("send button not found (quick_reply=%r)", quick_reply)
        return False
    try:
        await btn.click()
        await page.wait_for_timeout(2500)
    except Exception as exc:
        log.warning("send click failed: %s", exc)
        return False

    last = {}
    try:
        for _ in range(5):
            data = await _extract_messages(page)
            messages = data.get("messages", [])
            last = messages[-1] if messages else {}
            if _messages_contain_sent_text(messages, quick_reply or text):
                return True
            await page.wait_for_timeout(1000)
    except Exception as exc:
        log.warning("send verification failed to read current chat %s: %s", chat_id, exc)
        return False

    log.warning(
        "send verification failed for chat %s: last_is_me=%s last_author=%r last_text=%r",
        chat_id,
        bool(last.get("is_me")),
        last.get("author") or "",
        (last.get("text") or "")[:160],
    )
    return False


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


def chat_manual_send_callback_data(profile_name: str, chat_id: str, message_id: str) -> str:
    return _chat_callback_data("chat_send_any", profile_name, chat_id, message_id)


def build_suspicious_chat_reply_markup(profile_name: str, chat_id: str, message_id: str) -> dict:
    rows = [[{"text": "Открою сам", "url": f"{CHATIK_ROOT}/chat/{chat_id}"}]]
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
) -> dict:
    rows = [[{"text": "Открыть чат", "url": f"{CHATIK_ROOT}/chat/{chat_id}"}]]
    callback_data = (
        chat_manual_send_callback_data(profile_name, chat_id, message_id)
        if allow_any
        else chat_send_callback_data(profile_name, chat_id, message_id)
    )
    if len(callback_data.encode("utf-8")) <= 64:
        rows.append([{"text": "Отправить ответ", "callback_data": callback_data}])
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
    question = html.escape((message.get("text") or "").strip()[:900])
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


async def list_reply_candidates(hh_client, *, limit: int = 8, max_scan: int = 25) -> dict:
    # Return recent incoming HH chat messages that can be answered from Telegram buttons.
    if not hh_client._page:
        await hh_client.start(headless=True)
    page = hh_client._page
    await page.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1500)

    state = load_state()
    candidates: list[dict[str, Any]] = []
    summary = {
        "ok": True,
        "chats_scanned": 0,
        "chats_read": 0,
        "max_scan": max_scan,
        "limit": limit,
        "read_failures": 0,
        "candidates": candidates,
    }
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
        except Exception as exc:
            log.warning("candidate get_messages(%s) failed: %s", chat_id, exc)
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
        vacancy = dict(data.get("vacancy") or {})
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
        candidates.append({
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
        })

    return summary


async def _notify_one_chat_result(notifier, detail: dict) -> None:
    if notifier is None:
        return
    vac = detail.get("vacancy") or {}
    title = html.escape((vac.get("title") or "—")[:160])
    company = html.escape((vac.get("company") or "—")[:160])
    question = html.escape((detail.get("question") or "")[:450])
    answer = html.escape((detail.get("answer") or "")[:850])
    raw_chat_id = str(detail.get("chat_id") or "")
    raw_message_id = str(detail.get("message_id") or "")
    chat_id = html.escape(raw_chat_id)
    if detail.get("sent"):
        caption = (
            "<b>Ответил в подозрительном HR-чате</b>\n"
            f"{title} @ {company}\n\n"
            f"<i>{question}</i>\n\n"
            f"{answer}\n\n"
            f"<a href='{CHATIK_ROOT}/chat/{chat_id}'>Открыть чат</a>"
        )
        await notifier.send_message_with_markup(caption)
        return
    preview = detail.get("preview") or {}
    caption = (
        "<b>ИИ подготовил ответ</b>\n"
        f"{title} @ {company}\n\n"
        f"<i>{question}</i>\n\n"
        f"{answer}\n\n"
        f"<a href='{CHATIK_ROOT}/chat/{chat_id}'>Открыть чат</a>"
    )
    markup = (
        build_chat_answer_preview_markup(
            _active_profile_name(),
            raw_chat_id,
            raw_message_id,
            allow_any=bool(detail.get("manual_any")),
        )
        if raw_chat_id and raw_message_id
        else None
    )
    screenshot_path = preview.get("screenshot_path") or ""
    if screenshot_path:
        await notifier.send_photo(screenshot_path, caption=caption, reply_markup=markup)
    else:
        await notifier.send_message_with_markup(caption, reply_markup=markup)


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
            if key not in seen:
                return {"message": msg, "form_url": form_url, "key": key}
    return {}


def _remember_google_form_preview(chat_state: dict, key: str, detail: dict) -> None:
    remember_google_form_preview(chat_state, key, detail)


async def _notify_google_form_failure(notifier, *, chat_id: str, vacancy: dict, message: dict, form_url: str, error: str) -> bool:
    if notifier is None:
        return False
    title = html.escape((vacancy.get("title") or "Google Form")[:160])
    company = html.escape((vacancy.get("company") or "—")[:160])
    question = html.escape((message.get("text") or "")[:500])
    safe_form_url = html.escape(form_url or "")
    safe_chat_id = html.escape(str(chat_id or ""))
    safe_error = html.escape((error or "unknown error")[:300])
    caption = (
        "⚠️ <b>Google Form не удалось подготовить</b>\n"
        f"{title} @ {company}\n\n"
        f"<i>{question}</i>\n\n"
        f"Ошибка: {safe_error}\n"
        f"<a href='{safe_form_url}'>Открыть форму</a> · "
        f"<a href='{CHATIK_ROOT}/chat/{safe_chat_id}'>Открыть чат</a>"
    )
    return await notifier.send_message_with_markup(caption)


async def _prepare_google_form_preview_from_message(page, *, chat_id: str, vacancy: dict, message: dict, form_url: str, notifier) -> dict:
    import google_form_filler as gforms

    form_page = await page.context.new_page()
    try:
        detail = await gforms.preview_form(
            form_page,
            form_url,
            profile_name=_active_profile_name(),
            chat_id=chat_id,
            message_id=str(message.get("id") or ""),
            vacancy=vacancy,
            source_message=message.get("text") or "",
            notify=bool(notifier),
        )
        if not detail.get("ok"):
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
            )
    except Exception as exc:
        log.warning("google form preview failed for chat %s: %s", chat_id, exc)
        detail = {
            "ok": False,
            "message": f"{type(exc).__name__}: {exc}",
            "chat_id": chat_id,
            "message_id": str(message.get("id") or ""),
            "form_url": form_url,
            "vacancy": vacancy,
            "status": "preview_failed",
        }
        await _notify_google_form_failure(
            notifier,
            chat_id=chat_id,
            vacancy=vacancy,
            message=message,
            form_url=form_url,
            error=detail["message"],
        )
    finally:
        try:
            await form_page.close()
        except Exception:
            pass
    return detail


async def process_one(
    hh_client,
    chat_id: str,
    *,
    message_id: str = "",
    allow_suspicious: bool = False,
    allow_any: bool = False,
    dry_run: bool | None = None,
    notify: bool = False,
) -> dict:
    """Generate/send one reply for a specific chat message after human approval."""
    if dry_run is None:
        dry_run = not bool(int(os.getenv("HH_CHAT_AUTOSEND", "0") or 0))

    if not hh_client._page:
        await hh_client.start(headless=True)
    page = hh_client._page
    await page.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1500)

    data = await get_messages(page, chat_id)
    messages = data.get("messages", [])
    target = _find_message(messages, message_id)
    if not target:
        return {"ok": False, "message": "message not found", "chat_id": chat_id}
    if target.get("is_me"):
        return {"ok": False, "message": "target message is ours", "chat_id": chat_id}

    target_id = str(target.get("id") or "")
    state = load_state()
    chat_state = state.setdefault(str(chat_id), {})
    if chat_state.get("last_replied_msg_id") == target_id:
        return {"ok": True, "already_replied": True, "message": "already replied", "chat_id": chat_id}

    is_regular_ai = bool(target.get("is_ai"))
    is_approved_suspicious = allow_suspicious and bool(target.get("is_ai_suspect"))
    is_manual_any = bool(allow_any) and not bool(target.get("is_me"))
    if not is_regular_ai and not is_approved_suspicious and not is_manual_any:
        return {
            "ok": False,
            "message": "target message is not AI/suspicious/manual-approved screening",
            "chat_id": chat_id,
            "question": (target.get("text") or "")[:300],
        }

    try:
        from hh_client import _load_resume_text
    except Exception:
        _load_resume_text = lambda: ""
    resume_text = _load_resume_text()
    vacancy = data.get("vacancy", {})
    answer = await generate_answer(
        messages,
        vacancy,
        resume_text,
        question_message=target if (is_approved_suspicious or is_manual_any) else None,
        question_kind=(
            "подозрительное HR-сообщение"
            if is_approved_suspicious
            else "ручное HR-сообщение"
            if is_manual_any
            else "AI-помощник"
        ),
    )
    if not answer:
        return {"ok": False, "message": "LLM did not produce answer", "chat_id": chat_id}

    detail = {
        "ok": True,
        "chat_id": chat_id,
        "message_id": target_id,
        "vacancy": vacancy,
        "question": (target.get("text") or "")[:500],
        "answer": answer,
        "dry_run": dry_run,
        "suspicious": is_approved_suspicious,
        "manual_any": is_manual_any,
    }
    if dry_run:
        preview = await fill_and_preview(page, chat_id, answer)
        detail["preview"] = preview
        detail["sent"] = False
    else:
        ok = await send_message(page, chat_id, answer)
        detail["sent"] = ok
        if ok:
            chat_state["last_replied_msg_id"] = target_id
            chat_state["replies_count"] = int(chat_state.get("replies_count", 0)) + 1
            chat_state["last_reply_at"] = time.time()
            save_state(state)
        else:
            detail["ok"] = False
            detail["message"] = "send failed"

    if notify:
        try:
            import notifier
        except Exception:
            notifier = None
        await _notify_one_chat_result(notifier, detail)
    return detail


# ── Main process loop ──────────────────────────────────────────────────────

async def process_all(hh_client, dry_run: bool | None = None, max_replies_per_chat: int | None = None) -> dict:
    """Main entry: polling + reply.

    dry_run: если None — берётся из env HH_CHAT_AUTOSEND (0 = dry-run).
    """
    if dry_run is None:
        dry_run = not bool(int(os.getenv("HH_CHAT_AUTOSEND", "0") or 0))
    max_replies = max_replies_per_chat or int(os.getenv("HH_CHAT_MAX_REPLIES_PER_CHAT", "5"))

    if not hh_client._page:
        await hh_client.start(headless=True)
    page = hh_client._page

    # Загрузить cookies через домашнюю страницу hh.ru
    await page.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1500)

    state = load_state()
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

    chats = await list_chats(page)
    summary["chats_scanned"] = len(chats)
    log.info("found %d chats total", len(chats))

    try:
        from hh_client import _load_resume_text  # late import
    except Exception:
        _load_resume_text = lambda: ""
    resume_text = _load_resume_text()

    try:
        import notifier
    except Exception:
        notifier = None

    for chat in chats:
        chat_id = chat["chat_id"]
        preview_text = chat.get("preview") or ""
        if _is_application_only_preview(preview_text):
            log.debug("chat %s: application-only placeholder, skip", chat_id)
            continue
        if _is_blocked_company_preview(preview_text):
            log.info("chat %s: blocked company preview, skip", chat_id)
            summary["skipped"] += 1
            continue
        chat_state = state.setdefault(chat_id, {})
        replies_so_far = int(chat_state.get("replies_count", 0))

        try:
            data = await get_messages(page, chat_id)
        except Exception as exc:
            log.warning(
                "get_messages(%s) failed: %s; preview=%r",
                chat_id,
                exc,
                (chat.get("preview") or "")[:220],
            )
            summary["read_failures"] += 1
            continue
        msgs = data.get("messages", [])

        form_item = _find_unseen_google_form_message(msgs, chat_state)
        if form_item:
            form_msg = form_item["message"]
            form_url = form_item["form_url"]
            vac = dict(data.get("vacancy", {}) or {})
            preview_text = (chat.get("preview") or "").strip()
            if preview_text and (not vac.get("title") or _normalize_ai_marker_text(vac.get("title") or "") == "перейти"):
                vac["title"] = preview_text
            summary["google_forms_found"] += 1
            detail = await _prepare_google_form_preview_from_message(
                page,
                chat_id=chat_id,
                vacancy=vac,
                message=form_msg,
                form_url=form_url,
                notifier=notifier,
            )
            _remember_google_form_preview(chat_state, form_item["key"], detail)
            save_state(state)
            if detail.get("ok"):
                summary["google_forms_prepared"] += 1
            else:
                summary["google_forms_failed"] += 1
            summary["details"].append({
                "chat_id": chat_id,
                "vacancy": vac.get("title", ""),
                "company": vac.get("company", ""),
                "question": (form_msg.get("text") or "")[:300],
                "google_form": True,
                "form_url": detail.get("form_url") or form_url,
                "token": detail.get("token") or "",
                "ok": bool(detail.get("ok")),
                "message": detail.get("message") or detail.get("status") or "preview",
            })
            continue

        if replies_so_far >= max_replies:
            log.info("chat %s: max replies (%d) reached, skip", chat_id, max_replies)
            summary["skipped"] += 1
            continue

        # последнее сообщение — оно должно быть входящим. Если оно похоже на
        # автоматический HR-скрининг без явного AI-маркера, не отвечаем сами:
        # отправляем человеку approval-карточку в Telegram.
        last = msgs[-1] if msgs else None
        if not last:
            log.info("chat %s: no messages, skip", chat_id)
            summary["skipped"] += 1
            continue
        if last.get("is_me"):
            log.info("chat %s: latest message is ours, skip", chat_id)
            summary["skipped"] += 1
            continue

        last_id = str(last.get("id") or "")
        if chat_state.get("last_replied_msg_id") == last_id:
            log.info("chat %s: already replied to latest message %s, skip", chat_id, last_id)
            summary["skipped"] += 1
            continue

        if last.get("is_ai_suspect") and not last.get("is_ai"):
            summary["suspicious"] += 1
            if chat_state.get("last_suspicious_msg_id") == last_id:
                log.info("chat %s: suspicious message %s already notified, skip", chat_id, last_id)
                summary["skipped"] += 1
                continue
            vac = dict(data.get("vacancy", {}) or {})
            preview_text = (chat.get("preview") or "").strip()
            if preview_text and (not vac.get("title") or _normalize_ai_marker_text(vac.get("title") or "") == "перейти"):
                vac["title"] = preview_text
            profile_name = _active_profile_name()
            notified = False
            if notifier:
                try:
                    notified = await notify_suspicious_screening_message(
                        notifier,
                        chat_id=chat_id,
                        vacancy=vac,
                        message=last,
                        profile_name=profile_name,
                    )
                except Exception as exc:
                    log.warning("notify suspicious chat %s failed: %s", chat_id, exc)
            if notified:
                summary["suspicious_notified"] += 1
                chat_state["last_suspicious_msg_id"] = last_id
                chat_state["last_suspicious_at"] = time.time()
                save_state(state)
            summary["details"].append({
                "chat_id": chat_id,
                "vacancy": vac.get("title", ""),
                "company": vac.get("company", ""),
                "question": (last.get("text") or "")[:300],
                "suspicious": True,
                "notified": notified,
            })
            continue

        if not any(m.get("is_ai") for m in msgs):
            continue
        summary["with_ai"] += 1

        if not last.get("is_ai"):
            log.info("chat %s: latest message is not AI, skip", chat_id)
            summary["skipped"] += 1
            continue  # последний от человека-HR — не лезем

        # генерируем ответ
        answer = await generate_answer(msgs, data.get("vacancy", {}), resume_text)
        if not answer:
            log.info("chat %s: LLM не дал ответ", chat_id)
            summary["skipped"] += 1
            continue
        summary["answers_drafted"] += 1

        vac = data.get("vacancy", {})
        detail = {
            "chat_id": chat_id,
            "vacancy": vac.get("title", ""),
            "company": vac.get("company", ""),
            "question": (last.get("text") or "")[:300],
            "answer": answer,
        }

        if dry_run:
            # Превью: набираем текст без отправки + скрин
            preview = await fill_and_preview(page, chat_id, answer)
            detail["dry_run"] = True
            detail["preview"] = preview
            if notifier:
                try:
                    caption = (
                        f"🤖 <b>Чат с AI-помощником (DRY-RUN)</b>\n"
                        f"📋 {vac.get('title', '—')} @ {vac.get('company', '—')}\n\n"
                        f"❓ <i>{(last.get('text') or '')[:400]}</i>\n\n"
                        f"💬 <b>Готов ответить:</b>\n{answer[:800]}\n\n"
                        f"Чтобы включить авто-отправку: <code>HH_CHAT_AUTOSEND=1</code>"
                    )
                    if preview.get("screenshot_path"):
                        await notifier.send_photo(preview["screenshot_path"], caption=caption)
                    else:
                        await notifier.send_message_with_markup(caption)
                except Exception as exc:
                    log.warning("notify dry-run failed: %s", exc)
        else:
            # реальная отправка
            ok = await send_message(page, chat_id, answer)
            detail["sent"] = ok
            if ok:
                summary["answers_sent"] += 1
                chat_state["last_replied_msg_id"] = last_id
                chat_state["replies_count"] = replies_so_far + 1
                chat_state["last_reply_at"] = time.time()
                save_state(state)
                if notifier:
                    try:
                        caption = (
                            f"🤖 <b>Ответил в чате</b>\n"
                            f"📋 {vac.get('title', '—')} @ {vac.get('company', '—')}\n\n"
                            f"❓ <i>{(last.get('text') or '')[:300]}</i>\n\n"
                            f"💬 {answer[:800]}\n\n"
                            f"<a href='https://chatik.hh.ru/chat/{chat_id}'>Открыть чат</a>"
                        )
                        await notifier.send_message_with_markup(caption)
                    except Exception as exc:
                        log.warning("notify sent failed: %s", exc)
            else:
                summary["skipped"] += 1

        summary["details"].append(detail)
        # cooldown между ответами в разных чатах
        await asyncio.sleep(int(os.getenv("HH_CHAT_REPLY_COOLDOWN_S", "30")))

    return summary
