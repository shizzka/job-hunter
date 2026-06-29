from __future__ import annotations

import contextlib
import hashlib
import html
import httpx
import json
import logging
import os
import re
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import config
from llm_client import get_llm_client
from llm_utils import parse_llm_json

log = logging.getLogger("google_form_filler")

CALLBACK_GOOGLE_FORM_PREVIEW = "gform_preview"
CALLBACK_GOOGLE_FORM_SUBMIT = "gform_submit"
STATE_FILENAME = "google_form_previews.json"
FORM_URL_RE = re.compile(r"https?://[^\s<>'\")]+", re.I)


def _safe_profile(profile_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", profile_name or "default")


def _state_path() -> str:
    return os.path.join(config.JOB_HUNTER_HOME, STATE_FILENAME)


def _load_state() -> dict:
    path = _state_path()
    if not os.path.exists(path):
        return {"items": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"items": {}}
    except Exception as exc:
        log.warning("google form state read failed: %s", exc)
        return {"items": {}}


def _save_state(state: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _is_google_form_url(value: str) -> bool:
    low = unquote(str(value or "")).lower()
    return "docs.google.com/forms" in low or "forms.gle/" in low


def _strip_url_tail(value: str) -> str:
    return (value or "").strip().rstrip(".,;:!?)>]}\"'")


def normalize_google_form_url(raw: str) -> str:
    value = html.unescape(_strip_url_tail(str(raw or "")))
    if not value:
        return ""
    decoded = unquote(value)
    try:
        parsed = urlparse(decoded)
        params = parse_qs(parsed.query)
    except Exception:
        return decoded if _is_google_form_url(decoded) else ""
    for key in ("url", "u", "to", "target", "backurl", "redirect", "q"):
        for candidate in params.get(key, []):
            candidate = unquote(_strip_url_tail(candidate))
            if _is_google_form_url(candidate):
                return candidate
    if _is_google_form_url(decoded):
        return decoded
    return ""


def extract_google_form_urls(text: str = "", links: list[dict] | list[str] | None = None) -> list[str]:
    found: list[str] = []
    for match in FORM_URL_RE.finditer(text or ""):
        url = normalize_google_form_url(match.group(0))
        if url and url not in found:
            found.append(url)
    for item in links or []:
        candidates = [item.get("href") or "", item.get("text") or ""] if isinstance(item, dict) else [str(item)]
        for candidate in candidates:
            url = normalize_google_form_url(candidate)
            if url and url not in found:
                found.append(url)
    return found


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
    seed = f"{form_url}|{chat_id}|{message_id}|{time.time()}|{os.getpid()}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


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


async def extract_form_questions(page) -> list[dict]:
    await page.wait_for_selector('form, div[role="listitem"]', timeout=30000)
    questions = await page.evaluate("""() => {
        const clean = (value) => String(value || '').replace(/\\u00a0/g, ' ').replace(/[ \\t]+/g, ' ').trim();
        const unique = (values) => [...new Set(values.map(clean).filter(Boolean))];
        const items = [...document.querySelectorAll('div[role="listitem"]')];
        const out = [];
        for (let idx = 0; idx < items.length; idx++) {
            const item = items[idx];
            const radios = [...item.querySelectorAll('[role="radio"]')];
            const checks = [...item.querySelectorAll('[role="checkbox"]')];
            const textFields = [...item.querySelectorAll('textarea, input[type="text"], input[type="email"], input[type="number"], input[type="url"], input[type="tel"]')]
                .filter(el => !el.disabled && el.type !== 'hidden');
            const listboxes = [...item.querySelectorAll('[role="listbox"]')];
            if (!radios.length && !checks.length && !textFields.length && !listboxes.length) continue;

            const heading = item.querySelector('[role="heading"], .M7eMe');
            let question = clean(heading ? heading.innerText : '');
            const optionLabels = unique([...radios, ...checks].map(el => el.getAttribute('aria-label') || el.innerText));
            if (!question) {
                const lines = clean(item.innerText).split('\\n').map(clean).filter(Boolean);
                question = lines.find(line => line !== '*' && !optionLabels.includes(line) && !/обязательный вопрос/i.test(line)) || '';
            }
            let type = 'text';
            if (radios.length) type = 'radio';
            else if (checks.length) type = 'checkbox';
            else if (listboxes.length) type = 'select';
            const requiredHints = unique([...item.querySelectorAll('[aria-label], [data-tooltip], [title]')]
                .map(el => [el.getAttribute('aria-label'), el.getAttribute('data-tooltip'), el.getAttribute('title')].join(' ')));
            const requiredStar = [...item.querySelectorAll('span, div')].some(el => {
                const cls = String((el.className && el.className.baseVal) || el.className || '');
                const label = el.getAttribute('aria-label') || '';
                return clean(el.innerText) === '*' && /vnumgf|required|обяз/i.test(`${cls} ${label}`);
            });
            const required = /обязательный вопрос|required/i.test(`${item.innerText || ''} ${requiredHints.join(' ')}`) || requiredStar;
            out.push({
                index: out.length,
                dom_index: idx,
                question: question.replace(/\\s+\\*$/, ''),
                type,
                required,
                options: optionLabels,
            });
        }
        return out;
    }""")
    return [
        q for q in questions
        if (q.get("question") or "").strip()
        and not _looks_like_form_info_block(q.get("question") or "")
    ]


def _looks_like_form_info_block(question_text: str) -> bool:
    text = _norm(question_text)
    if not text:
        return False
    return (
        text.startswith("благодарим за заполнение анкеты")
        or ("мы внимательно рассматриваем каждую анкету" in text and "спасибо за уделенное время" in text)
        or ("если вы не получили ответ" in text and "это будет означать" in text)
    )


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
    return any(marker in text for marker in ("telegram", "телеграм", "тг", "tg"))


def _asks_for_resume_url(question_text: str) -> bool:
    text = _norm(question_text)
    return "резюме" in text and any(marker in text for marker in ("ссыл", "url", "link", "продубли", "прикреп"))


def _apply_contact_overrides(questions: list[dict], answers: list[dict]) -> list[dict]:
    from prompt_blocks import get_candidate_contacts

    contacts = get_candidate_contacts()
    telegram = str(contacts.get("telegram") or "").strip()
    resume_url = str(contacts.get("resume_url") or "").strip()
    if not telegram and not resume_url:
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
        if telegram and _asks_for_telegram(qtext):
            answer_map[idx] = {
                "index": idx,
                "answer": telegram,
                "options": [],
                "skip": False,
                "confidence": "high",
                "source": "contact_override",
            }
            changed = True
        elif resume_url and _asks_for_resume_url(qtext):
            answer_map[idx] = {
                "index": idx,
                "answer": resume_url,
                "options": [],
                "skip": False,
                "confidence": "high",
                "source": "contact_override",
            }
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


def _answer_has_value(answer: dict) -> bool:
    if not isinstance(answer, dict) or answer.get("skip"):
        return False
    if str(answer.get("answer") or "").strip():
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


def _required_option_fallback(options: list[str], question_text: str = "") -> list[str]:
    clean_options = [str(option or "").strip() for option in options or [] if str(option or "").strip()]
    if not clean_options:
        return []
    preferred_markers = (
        "qa", "тест", "гибрид", "другое", "соглас", "готов", "да", "подходит", "интерес",
    )
    for marker in preferred_markers:
        for option in clean_options:
            if marker in _norm(option):
                return [option]
    negative_markers = ("нет", "не готов", "не подходит", "отказ", "не рассматри", "не интерес")
    for option in clean_options:
        option_norm = _norm(option)
        if not any(marker in option_norm for marker in negative_markers):
            return [option]
    return [clean_options[0]]


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


def _prepare_form_answers(questions: list[dict], answers: list[dict]) -> list[dict]:
    answers = _apply_contact_overrides(questions, answers)
    return _apply_required_overrides(questions, answers)


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


async def fill_form(page, questions: list[dict], answers: list[dict]) -> dict:
    items = await page.query_selector_all('div[role="listitem"]')
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
                filled.append({"index": idx, "type": qtype, "answer": value})
                continue

            desired_values = list(answer.get("options") or []) or [str(answer.get("answer") or "")]
            options = list(question.get("options") or [])
            selected = []
            option_handles = await item.query_selector_all('[role="radio"], [role="checkbox"]')
            for desired in desired_values:
                match = _best_option_match(options, str(desired))
                if not match:
                    continue
                for handle in option_handles:
                    label = (await handle.get_attribute("aria-label")) or (await handle.inner_text())
                    if _norm(label) == _norm(match):
                        await handle.click()
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
    questions = await extract_form_questions(page)
    if not questions:
        return {"ok": False, "message": "form questions not found", "form_url": form_url, "page_url": page.url}
    answers = await generate_form_answers(questions, vacancy=vacancy, source_message=source_message)
    if not answers:
        answers = _reuse_cached_answers(form_url, questions)
    answers = _prepare_form_answers(questions, answers)
    fill_result = await fill_form(page, questions, answers)
    token = _new_token(form_url, chat_id, message_id)
    shot_path = os.path.join(config.HH_STATE_DIR, f"google_form_preview_{token}.png")
    os.makedirs(os.path.dirname(shot_path), exist_ok=True)
    await _safe_screenshot(page, shot_path)
    detail = {
        "ok": True,
        "token": token,
        "form_url": form_url,
        "original_form_url": original_form_url,
        "page_url": page.url,
        "chat_id": chat_id,
        "message_id": message_id,
        "vacancy": vacancy or {},
        "source_message": source_message[:1500],
        "questions": questions,
        "answers": answers,
        "fill_result": fill_result,
        "screenshot_path": shot_path,
        "created_at": int(time.time()),
        "status": "preview",
        "profile_name": profile_name,
    }
    state = _load_state()
    items = state.setdefault("items", {})
    items[token] = detail
    cutoff = int(time.time()) - 7 * 24 * 3600
    for old_token, item in list(items.items()):
        if int(item.get("created_at") or 0) < cutoff:
            items.pop(old_token, None)
    _save_state(state)
    if notify:
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
    if not hh_client._page:
        await hh_client.start(headless=True)
    page = hh_client._page
    await page.goto(item["form_url"], wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)
    questions = await extract_form_questions(page)
    fill_result = await fill_form(page, questions, item.get("answers") or [])
    submitted = False
    buttons = await page.query_selector_all('div[role="button"], button')
    for button in buttons:
        text = _norm(await button.inner_text())
        if "отправить" in text or "submit" in text:
            await button.click()
            submitted = True
            break
    await page.wait_for_timeout(3000)
    shot_path = os.path.join(config.HH_STATE_DIR, f"google_form_submit_{token}.png")
    os.makedirs(os.path.dirname(shot_path), exist_ok=True)
    await _safe_screenshot(page, shot_path)
    page_text = ""
    try:
        page_text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        pass
    ok = submitted and any(
        marker in page_text.casefold()
        for marker in ("ответ записан", "response has been recorded", "ответ отправлен", "отправлен")
    )
    result = {
        "ok": bool(ok),
        "submitted": submitted,
        "token": token,
        "form_url": item.get("form_url"),
        "fill_result": fill_result,
        "screenshot_path": shot_path,
        "message": "submitted" if ok else "submit clicked, verification uncertain" if submitted else "submit button not found",
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
