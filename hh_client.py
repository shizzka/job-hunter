"""Playwright-based клиент для hh.ru — поиск, отклик, мониторинг приглашений."""
import json
import os
import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import urlencode
from openai import AsyncOpenAI
from playwright.async_api import async_playwright, BrowserContext, Page

import config
import proxy_utils

log = logging.getLogger("hh_client")
HH_AUTH_COOKIE_NAMES = {"hhtoken", "hhuid", "crypted_hhuid", "crypted_id"}
_question_answer_client: AsyncOpenAI | None = None


def _ensure_dirs():
    os.makedirs(os.path.dirname(config.HH_COOKIES_FILE), exist_ok=True)
    os.makedirs(config.HH_STATE_DIR, exist_ok=True)


def _load_cookies() -> list[dict] | None:
    if os.path.exists(config.HH_COOKIES_FILE):
        with open(config.HH_COOKIES_FILE) as f:
            return json.load(f)
    return None


def _save_cookies(cookies: list[dict]):
    _ensure_dirs()
    with open(config.HH_COOKIES_FILE, "w") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


def _absolute_hh_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{config.HH_BASE_URL}{url}"


def _normalize_text(value: str) -> str:
    return " ".join((value or "").split()).casefold()


def _compact_text(value: str) -> str:
    return "".join((value or "").split()).casefold()


def _looks_like_existing_hh_response(value: str) -> bool:
    text = _normalize_text(value)
    return (
        "вы откликнулись" in text
        or "уже отклик" in text
        or "отклик другим резюме" in text
        or "откликнуться повторно" in text
    )


def _looks_like_hh_apply_success(value: str) -> bool:
    text = _normalize_text(value)
    return (
        _looks_like_existing_hh_response(value)
        or "резюме доставлено" in text
        or "отклик отправлен" in text
        or "связаться с работодателем можно в чате" in text
    )


def _load_resume_text() -> str:
    try:
        if os.path.exists(config.RESUME_FILE):
            with open(config.RESUME_FILE, encoding="utf-8") as f:
                return f.read().strip()
    except Exception as exc:
        log.warning("Failed to read resume file %s: %s", config.RESUME_FILE, exc)
    return ""


def _extract_resume_salary_text(resume_text: str) -> str:
    if not resume_text:
        return ""

    match = re.search(r"^##\s*Зарплата\s*$\n+([^\n]+)", resume_text, flags=re.MULTILINE)
    if match:
        return match.group(1).strip()

    for line in resume_text.splitlines():
        stripped = line.strip()
        if "₽" in stripped or "руб" in stripped.casefold():
            return stripped
    return ""


def _extract_numeric_salary(value: str) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return ""
    if len(digits) > 6:
        digits = digits[:6]
    return digits


def _is_salary_question(value: str) -> bool:
    text = _normalize_text(value)
    return any(
        token in text
        for token in (
            "зарплат",
            "ожидан",
            "желаем",
            "доход",
            "оклад",
            "компенсац",
            "оплата труда",
            "сколько хотите",
        )
    )


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _strip_markdown_fence(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return text


def _anti_bot_label(kind: str) -> str:
    mapping = {
        "captcha": "captcha",
        "ddos_guard": "DDOS-GUARD",
        "browser_check": "проверка браузера",
        "rate_limit": "rate limit",
    }
    return mapping.get((kind or "").strip(), "anti-bot")


def _anti_bot_message(kind: str, suffix: str = "") -> str:
    message = f"hh.ru anti-bot ({_anti_bot_label(kind)})"
    if suffix:
        message = f"{message} {suffix}"
    return message


def _get_question_answer_client() -> AsyncOpenAI:
    global _question_answer_client
    if _question_answer_client is None:
        _question_answer_client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY or "no-key",
            http_client=proxy_utils.llm_http_client(),
        )
    return _question_answer_client


class HHClient:
    """Управляет браузерной сессией hh.ru."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._last_antibot_signal: dict | None = None

    async def start(self, headless: bool | None = None):
        """Запустить браузер и загрузить cookies."""
        _ensure_dirs()
        self._pw = await async_playwright().start()

        launch_opts = {
            "headless": headless if headless is not None else config.HEADLESS,
            "slow_mo": config.SLOW_MO,
        }
        # Прокси (Mihomo на 127.0.0.1:7897) — если hh.ru не грузится напрямую
        proxy_url = os.environ.get("HH_PROXY", config.BROWSER_PROXY)
        if proxy_url:
            launch_opts["proxy"] = {"server": proxy_url}
            log.info("Using proxy: %s", proxy_url)
        launch_opts["env"] = proxy_utils.browser_launch_env(proxy_url)

        self._browser = await self._pw.chromium.launch(**launch_opts)
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        cookies = _load_cookies()
        if cookies:
            await self._context.add_cookies(cookies)
            log.info("Loaded %d cookies", len(cookies))
        self._page = await self._context.new_page()

    async def stop(self):
        """Закрыть браузер."""
        if self._context:
            cookies = await self._context.cookies()
            _save_cookies(cookies)
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    def consume_antibot_signal(self) -> dict | None:
        signal = self._last_antibot_signal
        self._last_antibot_signal = None
        return signal

    def _remember_antibot_signal(self, kind: str, stage: str, message: str = "") -> None:
        self._last_antibot_signal = {
            "kind": (kind or "").strip(),
            "stage": (stage or "").strip(),
            "message": (message or _anti_bot_message(kind)).strip(),
            "url": (self._page.url if self._page else ""),
        }

    async def _click_with_fallbacks(self, element, label: str) -> bool:
        """Надёжный клик по элементу с fallback-стратегиями."""
        if not element:
            return False

        try:
            await element.evaluate(
                "el => el.scrollIntoView({block: 'center', inline: 'center'})"
            )
            await self._page.wait_for_timeout(300)
        except Exception:
            pass

        strategies = (
            ("normal", lambda: element.click(timeout=5000)),
            ("force", lambda: element.click(timeout=5000, force=True)),
            (
                "js",
                lambda: element.evaluate(
                    "el => { el.scrollIntoView({block: 'center', inline: 'center'}); el.click(); }"
                ),
            ),
        )

        for strategy_name, action in strategies:
            try:
                log.info("Clicking %s via %s strategy", label, strategy_name)
                await action()
                await self._page.wait_for_timeout(1000)
                return True
            except Exception as e:
                log.warning("%s click via %s failed: %s", label, strategy_name, e)

        return False

    async def _has_existing_response_ui(self) -> bool:
        """Проверить UI hh.ru на признак уже отправленного отклика."""
        selectors = (
            "[data-qa*='responded']",
            "[data-qa='already-responded-text']",
            "button:has-text('Вы откликнулись')",
            "a:has-text('Вы откликнулись')",
            "text='Вы откликнулись'",
            "button:has-text('Отклик другим резюме')",
            "a:has-text('Отклик другим резюме')",
            "button:has-text('Откликнуться повторно')",
            "a:has-text('Откликнуться повторно')",
        )
        try:
            for selector in selectors:
                marker = await self._page.query_selector(selector)
                if marker:
                    return True
            body_text = await self._page_text(limit=8000)
            return _looks_like_existing_hh_response(body_text)
        except Exception as e:
            log.debug("Existing response UI check failed: %s", e)
            return False

    async def _page_text(self, limit: int = 12000) -> str:
        try:
            return await self._page.evaluate(
                f"() => document.body.innerText.slice(0, {int(limit)})"
            )
        except Exception:
            return ""

    async def _apply_success_detected(self) -> bool:
        selectors = (
            "[data-qa*='responded']",
            "[data-qa='already-responded-text']",
            "[data-qa='vacancy-response-success-standard-notification']",
            "[data-qa*='success-standard-notification']",
            "button:has-text('Вы откликнулись')",
            "a:has-text('Вы откликнулись')",
            "text='Вы откликнулись'",
            "text='Резюме доставлено'",
            "text='Отклик отправлен'",
            "text='Связаться с работодателем можно в чате'",
        )
        try:
            for selector in selectors:
                marker = await self._page.query_selector(selector)
                if marker:
                    return True
        except Exception as e:
            log.debug("Apply success selector check failed: %s", e)

        current_url = self._page.url or ""
        if "/negotiations" in current_url:
            return True

        page_text = await self._page_text(limit=12000)
        return _looks_like_hh_apply_success(page_text)

    async def _response_requires_questions(self, current_url: str = "") -> bool:
        page_url = (current_url or self._page.url or "").lower()
        if "vacancy_response_question" in page_url:
            return True

        selectors = (
            "h1:has-text('Ответьте на вопросы')",
            "h2:has-text('Ответьте на вопросы')",
            "text='Ответьте на вопросы'",
            "text='Для отклика необходимо ответить на несколько вопросов работодателя'",
        )
        try:
            for selector in selectors:
                marker = await self._page.query_selector(selector)
                if marker:
                    return True
        except Exception as exc:
            log.debug("Question flow selector check failed: %s", exc)

        page_text = _normalize_text(await self._page_text(limit=12000))
        return (
            "ответьте на вопросы" in page_text
            or "для отклика необходимо ответить на несколько вопросов работодателя" in page_text
        )

    async def _inspect_employer_questions(self) -> dict:
        try:
            result = await self._page.evaluate(
                """() => {
                    /* codex:auto-question-inspect */
                    const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
                    const visible = (el) => {
                        if (!el || el.disabled) return false;
                        const style = window.getComputedStyle(el);
                        if (!style || style.display === "none" || style.visibility === "hidden") return false;
                        const rect = el.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    };
                    const seen = new Set();
                    let seq = 0;
                    let unsupported = 0;
                    const unsupportedItems = [];
                    const fields = [];
                    const selectors = "textarea, select, input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='image']):not([type='file'])";
                    const nodes = Array.from(document.querySelectorAll(selectors)).filter(visible);

                    const collectPromptTexts = (el) => {
                        const parts = [];
                        const push = (value) => {
                            const text = clean(value);
                            if (text && !parts.includes(text)) {
                                parts.push(text);
                            }
                        };
                        push(el.closest("fieldset")?.querySelector("legend")?.innerText || "");
                        push(el.getAttribute("aria-label") || "");
                        push(el.getAttribute("placeholder") || "");

                        let prev = el.previousElementSibling;
                        for (let depth = 0; prev && depth < 4; depth += 1) {
                            push(prev.innerText || "");
                            prev = prev.previousElementSibling;
                        }

                        let node = el.parentElement;
                        for (let depth = 0; node && depth < 4; depth += 1) {
                            push(node.innerText || "");
                            node = node.parentElement;
                        }
                        return parts;
                    };

                    const describeField = (el) => {
                        const labels = el.labels ? Array.from(el.labels).map((label) => clean(label.innerText)).filter(Boolean) : [];
                        const promptParts = collectPromptTexts(el);
                        const placeholder = clean(el.getAttribute("placeholder") || "");
                        const questionText = clean(
                            [labels.join(" "), ...promptParts]
                                .filter(Boolean)
                                .join(" ")
                        ).slice(0, 500);

                        return {
                            question_text: questionText,
                            placeholder,
                        };
                    };

                    for (const el of nodes) {
                        const tag = (el.tagName || "").toLowerCase();
                        const inputType = tag === "input"
                            ? ((el.getAttribute("type") || "text").toLowerCase())
                            : tag;

                        if (inputType === "radio" || inputType === "checkbox" || tag === "select") {
                            const groupKey = `${tag}:${el.getAttribute("name") || el.id || el.value || seq}`;
                            if (!seen.has(groupKey)) {
                                seen.add(groupKey);
                                unsupported += 1;
                                const optionText = tag === "select"
                                    ? Array.from(el.querySelectorAll("option")).map((option) => clean(option.innerText)).filter(Boolean)
                                    : Array.from(document.querySelectorAll(`${tag}[name="${el.getAttribute("name") || ""}"]`))
                                        .map((option) => clean(option.closest("label")?.innerText || option.value || ""))
                                        .filter(Boolean);
                                const promptParts = collectPromptTexts(el);
                                unsupportedItems.push({
                                    control: tag,
                                    input_type: inputType,
                                    question_text: clean(promptParts.join(" ")).slice(0, 500),
                                    options: optionText.slice(0, 8),
                                });
                            }
                            continue;
                        }

                        const autoId = el.getAttribute("data-codex-auto-field-id") || `codex-auto-field-${++seq}`;
                        el.setAttribute("data-codex-auto-field-id", autoId);
                        const described = describeField(el);
                        fields.push({
                            field_id: autoId,
                            control: tag,
                            input_type: inputType,
                            question_text: described.question_text,
                            placeholder: described.placeholder,
                            max_length: Number(el.getAttribute("maxlength") || 0) || 0,
                        });
                    }

                    return {
                        page_text: clean(document.body.innerText || "").slice(0, 6000),
                        fields,
                        unsupported_fields: unsupported,
                        unsupported_items: unsupportedItems,
                    };
                }"""
            )
        except Exception as exc:
            log.warning("Question form inspection failed: %s", exc)
            return {"page_text": "", "fields": [], "unsupported_fields": 0, "unsupported_items": []}

        if not isinstance(result, dict):
            return {"page_text": "", "fields": [], "unsupported_fields": 0, "unsupported_items": []}
        result.setdefault("page_text", "")
        result.setdefault("fields", [])
        result.setdefault("unsupported_fields", 0)
        result.setdefault("unsupported_items", [])
        return result

    async def _fill_employer_question_answers(self, answers: list[dict]) -> dict:
        try:
            return await self._page.evaluate(
                """(plan) => {
                    /* codex:auto-question-fill */
                    const dispatch = (el, name) => {
                        el.dispatchEvent(new Event(name, { bubbles: true }));
                    };
                    const setValue = (el, value) => {
                        const tag = (el.tagName || "").toLowerCase();
                        const prototype = tag === "textarea"
                            ? window.HTMLTextAreaElement?.prototype
                            : window.HTMLInputElement?.prototype;
                        const descriptor = prototype ? Object.getOwnPropertyDescriptor(prototype, "value") : null;
                        if (descriptor && typeof descriptor.set === "function") {
                            descriptor.set.call(el, value);
                        } else {
                            el.value = value;
                        }
                        dispatch(el, "input");
                        dispatch(el, "change");
                    };

                    const result = { filled: 0, errors: [] };
                    for (const item of plan || []) {
                        const selector = `[data-codex-auto-field-id="${item.field_id}"]`;
                        const el = document.querySelector(selector);
                        if (!el) {
                            result.errors.push(`field ${item.field_id} not found`);
                            continue;
                        }
                        try {
                            el.focus();
                            setValue(el, String(item.answer ?? ""));
                            result.filled += 1;
                        } catch (err) {
                            result.errors.push(String(err));
                        }
                    }
                    return result;
                }""",
                answers,
            )
        except Exception as exc:
            log.warning("Question form fill failed: %s", exc)
            return {"filled": 0, "errors": [str(exc)]}

    async def _submit_employer_questions(self) -> bool:
        if await self._submit_response_form_via_dom():
            return True

        selectors = (
            "[data-qa='vacancy-response-submit-popup']",
            "[data-qa='vacancy-response-letter-submit']",
            "button[data-qa*='submit']",
            "button:has-text('Отправить')",
            "button:has-text('Продолжить')",
            "button:has-text('Дальше')",
            "button:has-text('Откликнуться')",
        )
        for selector in selectors:
            try:
                button = await self._page.query_selector(selector)
            except Exception:
                continue
            if button and await self._click_with_fallbacks(button, f"question_submit:{selector}"):
                return True
        return False

    async def _answer_question_with_llm(
        self,
        field: dict,
        resume_text: str,
        page_text: str = "",
    ) -> str | None:
        if not config.HH_AUTO_ANSWER_USE_LLM or not config.LLM_API_KEY or not resume_text.strip():
            return None

        question_text = (field.get("question_text") or field.get("placeholder") or "").strip()
        if not question_text:
            return None

        field_type = (field.get("input_type") or field.get("control") or "text").strip() or "text"
        max_chars = config.HH_AUTO_ANSWER_MAX_CHARS
        field_max_length = int(field.get("max_length") or 0)
        if field_max_length > 0:
            max_chars = min(max_chars, field_max_length)

        prompt = f"""Ты отвечаешь на вопрос работодателя на hh.ru от имени кандидата.

Используй только факты из резюме. Ничего не выдумывай.
Если из резюме нельзя ответить уверенно, верни status=skip.

Тип поля: {field_type}
Максимум символов: {max_chars}
Вопрос: {question_text}
Контекст формы: {_truncate_text(page_text, 1200) if page_text else "(нет)"}

Резюме кандидата:
{resume_text[:6000]}

Верни только JSON без markdown:
{{
  "status": "answer" | "skip",
  "answer": "..."
}}

Правила:
- для text/textarea: коротко и по делу, без приветствий, до {max_chars} символов;
- для number: только число, без слов и знаков валюты;
- если ответ неочевиден или в резюме нет фактов, верни status=skip."""

        try:
            client = _get_question_answer_client()
            response = await client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=200,
            )
            raw_text = response.choices[0].message.content or ""
            parsed = json.loads(_strip_markdown_fence(raw_text))
        except Exception as exc:
            log.warning("LLM question answer failed: %s", exc)
            return None

        if parsed.get("status") != "answer":
            return None

        answer = str(parsed.get("answer", "")).strip()
        if not answer:
            return None

        if field_type == "number":
            answer = _extract_numeric_salary(answer) if not answer.isdigit() else answer
            if not answer:
                return None
            return answer

        return _truncate_text(answer, max_chars)

    async def _try_auto_answer_questions(self) -> dict:
        if not config.HH_AUTO_ANSWER_SIMPLE_QUESTIONS:
            return {
                "handled": True,
                "ok": False,
                "message": "Требуются доп. вопросы работодателя — пропускаем (автоответ отключён)",
                "notes": [],
            }

        inspected = await self._inspect_employer_questions()
        fields = inspected.get("fields") or []
        unsupported_fields = int(inspected.get("unsupported_fields") or 0)
        unsupported_items = inspected.get("unsupported_items") or []
        total_questions = len(fields) + unsupported_fields
        page_text = inspected.get("page_text", "")

        if total_questions <= 0:
            return {
                "handled": True,
                "ok": False,
                "message": "Требуются доп. вопросы работодателя — пропускаем (не удалось разобрать поля формы)",
                "notes": [],
            }

        if unsupported_fields:
            unsupported_summary = []
            for item in unsupported_items[:2]:
                question_text = _truncate_text(item.get("question_text") or "неизвестный вопрос", 120)
                options = item.get("options") or []
                if options:
                    question_text = f"{question_text} [{', '.join(options[:3])}]"
                unsupported_summary.append(question_text)
            return {
                "handled": True,
                "ok": False,
                "message": "Требуются доп. вопросы работодателя — пропускаем (есть неподдерживаемые поля)",
                "notes": [
                    "автоответ пропущен: в форме есть select/radio/checkbox"
                    + (f" ({'; '.join(unsupported_summary)})" if unsupported_summary else "")
                ],
            }

        if total_questions > config.HH_AUTO_ANSWER_MAX_QUESTIONS:
            return {
                "handled": True,
                "ok": False,
                "message": "Требуются доп. вопросы работодателя — пропускаем (слишком много полей)",
                "notes": [f"автоответ пропущен: полей {total_questions}, лимит {config.HH_AUTO_ANSWER_MAX_QUESTIONS}"],
            }

        resume_text = _load_resume_text()
        salary_text = config.HH_AUTO_ANSWER_SALARY_TEXT or _extract_resume_salary_text(resume_text)
        salary_number = config.HH_AUTO_ANSWER_SALARY_NUMBER or _extract_numeric_salary(salary_text)

        answers = []
        notes = []

        for field in fields:
            question_text = (field.get("question_text") or field.get("placeholder") or "").strip()
            input_type = (field.get("input_type") or "text").strip().lower()

            if _is_salary_question(question_text):
                answer = salary_number if input_type == "number" else (salary_text or salary_number)
                if not answer:
                    return {
                        "handled": True,
                        "ok": False,
                        "message": "Требуются доп. вопросы работодателя — пропускаем (не найден ответ по зарплате)",
                        "notes": ["автоответ пропущен: в резюме нет явного зарплатного ориентира"],
                    }
                notes.append(f"автоответ hh: зарплатные ожидания -> {answer}")
            else:
                answer = await self._answer_question_with_llm(field, resume_text, page_text)
                if not answer:
                    short_question = _truncate_text(question_text or "вопрос по резюме", 100)
                    return {
                        "handled": True,
                        "ok": False,
                        "message": "Требуются доп. вопросы работодателя — пропускаем (нет уверенного ответа)",
                        "notes": [f"автоответ пропущен: {short_question}"],
                    }
                notes.append(f"автоответ hh: {_truncate_text(question_text or 'вопрос по резюме', 100)}")

            answers.append({"field_id": field["field_id"], "answer": answer})

        fill_result = await self._fill_employer_question_answers(answers)
        if int(fill_result.get("filled", 0)) != len(answers):
            return {
                "handled": True,
                "ok": False,
                "message": "Требуются доп. вопросы работодателя — пропускаем (не удалось заполнить форму)",
                "notes": notes + [f"ошибка заполнения: {', '.join(fill_result.get('errors', [])[:2])}"],
            }

        await self._page.wait_for_timeout(500)

        if not await self._submit_employer_questions():
            return {
                "handled": True,
                "ok": False,
                "message": "Требуются доп. вопросы работодателя — пропускаем (не удалось отправить форму)",
                "notes": notes,
            }

        await self._page.wait_for_timeout(4000)

        anti_bot_kind = await self._detect_anti_bot_kind()
        if anti_bot_kind:
            message = _anti_bot_message(anti_bot_kind, "после автоответа на вопросы")
            self._remember_antibot_signal(anti_bot_kind, "questions_submit", message)
            return {
                "handled": True,
                "ok": False,
                "message": message,
                "notes": notes,
                "anti_bot_kind": anti_bot_kind,
            }

        if await self._apply_success_detected() or await self._has_existing_response_ui():
            return {
                "handled": True,
                "ok": True,
                "message": "Отклик отправлен",
                "notes": notes,
            }

        if await self._response_requires_questions():
            return {
                "handled": True,
                "ok": False,
                "message": "Требуются доп. вопросы работодателя — пропускаем (форма не закрылась после автоответа)",
                "notes": notes,
            }

        return {
            "handled": True,
            "ok": False,
            "message": "Не удалось подтвердить отклик после автоответа на вопросы",
            "notes": notes,
        }

    async def _dismiss_magritte_dropdowns(self) -> None:
        popup_selectors = (
            "[data-magritte-drop-base-direction]",
            "[data-qa='drop-base']",
        )
        for _ in range(3):
            popup = None
            for selector in popup_selectors:
                popup = await self._page.query_selector(selector)
                if popup:
                    break
            if popup is None:
                return

            try:
                await self._page.keyboard.press("Escape")
            except Exception:
                pass
            await self._page.wait_for_timeout(200)

            popup = None
            for selector in popup_selectors:
                popup = await self._page.query_selector(selector)
                if popup:
                    break
            if popup is None:
                return

            try:
                await self._page.evaluate(
                    "() => document.activeElement && typeof document.activeElement.blur === 'function' && document.activeElement.blur()"
                )
            except Exception:
                pass
            await self._page.wait_for_timeout(100)

    async def _expand_cover_letter_input(self) -> bool:
        selectors = (
            "[data-qa='add-cover-letter']",
            "button[data-qa='add-cover-letter']",
            "button:has-text('Добавить сопроводительное')",
            "button:has-text('Приложить письмо')",
            "button:has-text('Добавить письмо')",
        )
        for selector in selectors:
            try:
                button = await self._page.query_selector(selector)
            except Exception:
                continue
            if not button:
                continue
            if await self._click_with_fallbacks(button, f"cover_letter_toggle:{selector}"):
                await self._page.wait_for_timeout(500)
                return True
        return False

    async def _submit_response_form_via_dom(self) -> bool:
        try:
            result = await self._page.evaluate(
                """() => {
                    const form = document.querySelector("form[name='vacancy_response']");
                    if (form && typeof form.requestSubmit === 'function') {
                        form.requestSubmit();
                        return true;
                    }
                    const button = document.querySelector("[data-qa='vacancy-response-submit-popup']");
                    if (button) {
                        button.click();
                        return true;
                    }
                    return false;
                }"""
            )
        except Exception as exc:
            log.debug("DOM submit fallback failed: %s", exc)
            return False
        return bool(result)

    async def _detect_anti_bot_kind(self) -> str:
        current_url = (self._page.url or "").lower()
        try:
            body_text = await self._page.evaluate(
                "() => document.body.innerText.slice(0, 3000)"
            )
        except Exception:
            body_text = ""
        if not isinstance(body_text, str):
            body_text = ""
        body_lower = body_text.lower()

        if (
            "ddos-guard" in current_url
            or "ddos-guard" in body_lower
            or "проверка браузера перед переходом на hh.ru" in body_lower
            or "не удалось проверить ваш браузер автоматически" in body_lower
            or "checking your browser before accessing" in body_lower
        ):
            return "ddos_guard"
        if "/account/captcha" in current_url:
            return "captcha"

        # Проверяем наличие iframe капчи (reCAPTCHA, hCaptcha, Yandex SmartCaptcha)
        try:
            captcha_frame = await self._page.query_selector(
                "iframe[src*='captcha'], "
                "iframe[src*='recaptcha'], "
                "iframe[src*='hcaptcha'], "
                "iframe[src*='smartcaptcha'], "
                "[class*='captcha' i], "
                "[id*='captcha' i], "
                "[data-qa='captcha']"
            )
            if captcha_frame:
                return "captcha"
        except Exception:
            pass

        if (
            "подтвердите, что вы не робот" in body_lower
            or "текст с картинки" in body_lower
            or "i'm not a robot" in body_lower
            or "verify you are human" in body_lower
        ):
            return "captcha"
        if (
            "проверка браузера" in body_lower
            or "checking your browser" in body_lower
            or "verify your browser" in body_lower
        ):
            return "browser_check"
        return ""

    async def _is_captcha_page(self) -> bool:
        return bool(await self._detect_anti_bot_kind())

    async def save_session(self):
        """Сохранить текущие cookies."""
        if self._context:
            cookies = await self._context.cookies()
            _save_cookies(cookies)
            log.info("Session saved (%d cookies)", len(cookies))

    async def has_auth_cookies(self) -> bool:
        """Проверить наличие auth-cookie без навигации страницы."""
        if not self._context:
            return False
        try:
            cookies = await self._context.cookies([config.HH_BASE_URL])
        except TypeError:
            cookies = await self._context.cookies()
        names = {(item.get("name") or "").casefold() for item in cookies or []}
        return "hhtoken" in names and bool(names & HH_AUTH_COOKIE_NAMES)

    # ── Авторизация ───────────────────────────────────────────────────────

    async def login_interactive(self, keep_open: bool = False):
        """
        Открыть браузер для ручного логина.
        Пользователь логинится сам, потом нажимаем Enter в терминале.
        Если keep_open=True — не закрывает браузер (для последующей загрузки резюме).
        """
        await self.start(headless=False)
        try:
            await self._page.goto(
                f"{config.HH_BASE_URL}/account/login",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception:
            # Даже если таймаут — страница могла частично загрузиться, продолжаем
            pass
        print("\n" + "=" * 60)
        print("Браузер открыт. Залогинься на hh.ru.")
        print("После успешного входа нажми Enter здесь...")
        print("=" * 60)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, input)
        await self.save_session()
        print("✅ Cookies сохранены!")
        if not keep_open:
            await self.stop()

    async def is_logged_in(self) -> bool:
        """Проверить залогинен ли пользователь."""
        try:
            await self._page.goto(f"{config.HH_BASE_URL}/applicant/resumes", wait_until="domcontentloaded", timeout=15000)
            await self._page.wait_for_timeout(2000)
            url = (self._page.url or "").casefold()
            # Если редиректнуло на логин — не залогинен
            if "/account/login" in url or "/auth/" in url:
                return False
            html = _compact_text(await self._page.content())
            if '"usertype":"anonymous"' in html or '"luxpagename":"forbiddenpage"' in html:
                return False
            # Проверяем наличие элемента резюме
            resumes = await self._page.query_selector_all("[data-qa='resume']")
            return len(resumes) > 0 or "/applicant/resumes" in url
        except Exception as e:
            log.warning("Login check failed: %s", e)
            return False

    async def is_logged_in_passive(self) -> bool:
        """Проверить логин без навигации текущей страницы."""
        if not self._page or self._page.is_closed():
            return False
        try:
            url = (self._page.url or "").casefold()
            if not url or "/account/login" in url or "/auth/" in url or "/captcha" in url:
                return False
            html = _compact_text(await self._page.content())
            if '"usertype":"anonymous"' in html or '"luxpagename":"forbiddenpage"' in html:
                return False
            return await self.has_auth_cookies()
        except Exception as e:
            log.warning("Passive login check failed: %s", e)
            return False

    # ── Поиск вакансий ────────────────────────────────────────────────────

    async def search_vacancies(self, query: str, page: int = 0,
                              area: int = 113, schedule: str = "") -> list[dict]:
        """
        Поиск вакансий по запросу. Возвращает список:
        [{"id": "...", "title": "...", "company": "...", "salary": "...",
          "url": "...", "snippet": "..."}]
        """
        params = {
            "text": query,
            "area": area,
            "page": page,
            "per_page": 20,
            "order_by": "publication_time",  # свежие первые
        }
        if config.SEARCH_EXPERIENCE:
            params["experience"] = config.SEARCH_EXPERIENCE
        if schedule:
            params["schedule"] = schedule
        if config.SEARCH_SALARY:
            params["salary"] = config.SEARCH_SALARY
        if config.SEARCH_ONLY_WITH_SALARY:
            params["only_with_salary"] = "true"

        url = f"{config.HH_BASE_URL}/search/vacancy?{urlencode(params)}"
        log.info("Searching: %s", url)

        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning("Search page nav issue: %s", e)

        await self._page.wait_for_timeout(4000)  # дать JS подгрузиться

        # Дебаг: скриншот поисковой выдачи (первый запрос)
        if page == 0:
            try:
                safe_query = "".join(c if c.isalnum() else "_" for c in query[:20])
                debug_path = os.path.join(config.HH_STATE_DIR, f"debug_search_{safe_query}.png")
                await self._page.screenshot(path=debug_path, full_page=True)
                debug_html = os.path.join(config.HH_STATE_DIR, f"debug_search_{safe_query}.html")
                html = await self._page.content()
                with open(debug_html, "w") as f:
                    f.write(html)
                log.info("Search debug saved: %s", debug_path)
            except Exception:
                pass

        # Anti-bot check перед парсингом
        anti_bot_kind = await self._detect_anti_bot_kind()
        if anti_bot_kind:
            message = _anti_bot_message(anti_bot_kind, "на поиске")
            self._remember_antibot_signal(anti_bot_kind, "search", message)
            log.warning("hh.ru anti-bot (%s) on search page: %s", anti_bot_kind, self._page.url)
            return []

        vacancies = []

        # Стратегия 1: data-qa селекторы (классический hh.ru)
        cards = await self._page.query_selector_all("[data-qa='serp-item']")
        log.info("Search strategy 1 (serp-item): %d cards", len(cards))

        # Стратегия 2: альтернативные селекторы
        if not cards:
            cards = await self._page.query_selector_all("[data-qa='vacancy-serp__vacancy']")
            log.info("Search strategy 2 (vacancy-serp__vacancy): %d cards", len(cards))

        # Стратегия 3: любые карточки с ссылкой на вакансию
        if not cards:
            cards = await self._page.query_selector_all("[data-qa*='serp-item'], [data-qa*='vacancy-serp']")
            log.info("Search strategy 3 (wildcard serp): %d cards", len(cards))

        # Стратегия 4: ищем по ссылкам на /vacancy/
        if not cards:
            log.info("All card strategies failed, falling back to link parsing")
            vacancy_links = await self._page.query_selector_all("a[href*='/vacancy/']")
            log.info("Found %d vacancy links on page", len(vacancy_links))
            seen_ids = set()
            for link in vacancy_links:
                try:
                    href = await link.get_attribute("href") or ""
                    if "/vacancy/" not in href:
                        continue
                    vid = href.split("/vacancy/")[-1].split("?")[0].split("/")[0]
                    if not vid or not vid.isdigit() or vid in seen_ids:
                        continue
                    seen_ids.add(vid)
                    title = (await link.inner_text()).strip()
                    if not title or len(title) < 3 or len(title) > 200:
                        continue
                    # Пытаемся найти родительский контейнер для доп. инфо
                    parent = await link.evaluate_handle("el => el.closest('[class*=\"vacancy\"], [class*=\"serp\"], article, section') || el.parentElement.parentElement")
                    company = ""
                    salary = "не указана"
                    snippet = ""
                    if parent:
                        full_text = await parent.evaluate("el => el.innerText")
                        lines = [l.strip() for l in full_text.split("\n") if l.strip()]
                        # Обычно: заголовок, зарплата, компания, ...
                        for line in lines:
                            if "₽" in line or "$" in line or "руб" in line.lower():
                                salary = line
                            elif line != title and not company and len(line) > 2:
                                company = line
                    vacancies.append({
                        "id": vid,
                        "title": title,
                        "company": company,
                        "salary": salary,
                        "url": href if href.startswith("http") else f"{config.HH_BASE_URL}{href}",
                        "snippet": snippet,
                    })
                except Exception as e:
                    log.debug("Link parse failed: %s", e)

            log.info("Found %d vacancies via link parsing for '%s'", len(vacancies), query)
            return vacancies

        for card in cards:
            try:
                vacancy = await self._parse_vacancy_card(card)
                if vacancy:
                    vacancies.append(vacancy)
            except Exception as e:
                log.debug("Failed to parse vacancy card: %s", e)

        log.info("Found %d vacancies for '%s'", len(vacancies), query)
        return vacancies

    async def _parse_vacancy_card(self, card) -> dict | None:
        """Парсит карточку вакансии из поисковой выдачи."""
        # Заголовок и ссылка
        title_el = await card.query_selector(
            "[data-qa='serp-item__title'], "
            "[data-qa='serp__vacancy-title'], "
            "a.serp-item__title, "
            "h2 a, h3 a"
        )
        if not title_el:
            return None

        title = (await title_el.inner_text()).strip()
        url = _absolute_hh_url(await title_el.get_attribute("href") or "")

        # Извлекаем ID из URL
        vacancy_id = ""
        if "/vacancy/" in url:
            parts = url.split("/vacancy/")
            if len(parts) > 1:
                vacancy_id = parts[1].split("?")[0].split("/")[0]

        # Компания
        company_el = await card.query_selector(
            "[data-qa='vacancy-serp__vacancy-employer'], "
            "[data-qa='serp-item__company'], "
            ".vacancy-serp-item__meta-info-company a"
        )
        company = (await company_el.inner_text()).strip() if company_el else "—"

        # Зарплата
        salary_el = await card.query_selector(
            "[data-qa='vacancy-serp__vacancy-compensation'], "
            "[data-qa='serp-item__compensation'], "
            ".vacancy-serp-item__sidebar"
        )
        salary = (await salary_el.inner_text()).strip() if salary_el else "не указана"

        # Сниппет (краткое описание)
        snippet_el = await card.query_selector(
            "[data-qa='vacancy-serp__vacancy_snippet_requirement'], "
            ".g-user-content"
        )
        snippet = (await snippet_el.inner_text()).strip() if snippet_el else ""

        # Дополнительный сниппет (обязанности)
        resp_el = await card.query_selector(
            "[data-qa='vacancy-serp__vacancy_snippet_responsibility']"
        )
        if resp_el:
            resp_text = (await resp_el.inner_text()).strip()
            if resp_text:
                snippet = f"{snippet}\n{resp_text}" if snippet else resp_text

        response_el = await card.query_selector(
            "[data-qa='vacancy-serp__vacancy_response'], "
            "a[href*='/applicant/vacancy_response']"
        )
        response_url = _absolute_hh_url(
            await response_el.get_attribute("href") or ""
        ) if response_el else ""

        return {
            "id": vacancy_id,
            "title": title,
            "company": company,
            "salary": salary,
            "url": url,
            "snippet": snippet,
            "response_url": response_url,
        }

    # ── Детали вакансии ───────────────────────────────────────────────────

    async def get_vacancy_details(self, vacancy_url: str) -> str:
        """Получить полный текст вакансии."""
        await self._page.goto(vacancy_url, wait_until="domcontentloaded", timeout=20000)
        await self._page.wait_for_timeout(2000)

        # Описание вакансии
        desc_el = await self._page.query_selector(
            "[data-qa='vacancy-description'], "
            ".vacancy-description, "
            ".vacancy-section"
        )
        if desc_el:
            return (await desc_el.inner_text()).strip()

        # Fallback: весь контент страницы
        body = await self._page.query_selector("main, .vacancy-body, article")
        if body:
            return (await body.inner_text()).strip()[:3000]

        return ""

    # ── Отклик на вакансию ────────────────────────────────────────────────

    async def apply_to_vacancy(
        self,
        vacancy_url: str,
        cover_letter: str = "",
        response_url: str = "",
        preferred_resume_title: str = "",
        preferred_resume_id: str = "",
    ) -> dict:
        """
        Откликнуться на вакансию.
        Возвращает {"ok": bool, "message": str}
        """
        vacancy_url = _absolute_hh_url(vacancy_url)
        response_url = _absolute_hh_url(response_url)

        async def save_debug_snapshot(prefix: str):
            try:
                debug_path = os.path.join(config.HH_STATE_DIR, f"{prefix}.png")
                debug_html = os.path.join(config.HH_STATE_DIR, f"{prefix}.html")
                await self._page.screenshot(path=debug_path)
                with open(debug_html, "w") as f:
                    f.write(await self._page.content())
            except Exception:
                pass

        cover_letter_filled = False
        auto_answer_notes: list[str] = []

        async def finalize_success(
            message: str,
            *,
            already_applied: bool = False,
            notes: list[str] | None = None,
        ) -> dict:
            if cover_letter and not cover_letter_filled and not already_applied:
                await self._fill_cover_letter_post_apply(cover_letter)
            result = {"ok": True, "message": message}
            if already_applied:
                result["already_applied"] = True
            if notes:
                result["notes"] = notes
            return result

        async def detect_response_controls():
            current_url = self._page.url
            response_header = await self._page.query_selector(
                "h1:has-text('Отклик на вакансию'), "
                "h2:has-text('Отклик на вакансию')"
            )
            questions_required = await self._response_requires_questions(current_url)
            resume_select = await self._page.query_selector(
                "[data-qa='resume-select'], "
                "[data-qa*='resume-item'], "
                "[data-qa='vacancy-response-popup-form-resume']"
            )
            letter_field = await self._page.query_selector(
                "[data-qa='vacancy-response-popup-form-letter-input'], "
                "textarea[name='letter'], "
                "textarea[data-qa*='letter'], "
                ".vacancy-response-popup textarea, "
                "textarea"
            )
            submit_btn = await self._page.query_selector(
                "[data-qa='vacancy-response-submit-popup'], "
                "[data-qa='vacancy-response-letter-submit'], "
                "button[data-qa*='submit'], "
                "[data-qa='vacancy-response-link-top-again'], "
                "[data-qa='vacancy-response-link-bottom-again'], "
                "[data-qa='vacancy-response-link-top'], "
                "[data-qa='vacancy-response-link-bottom'], "
                "a[data-qa*='response-link']"
            )
            if not submit_btn:
                # Some hh flows collapse back to the vacancy page after resume selection
                # and expose only a link-style "Откликнуться" control.
                submit_btn = await self._page.query_selector(
                    "button:has-text('Откликнуться'), "
                    "button:has-text('Отправить'), "
                    "a:has-text('Откликнуться'), "
                    "a:has-text('Отправить')"
            )
            return (
                current_url,
                response_header,
                questions_required,
                resume_select,
                letter_field,
                submit_btn,
            )

        async def refetch_response_controls():
            return await detect_response_controls()

        async def refetch_letter_field():
            for _ in range(3):
                (
                    _current_url,
                    _response_header,
                    _questions_required,
                    _resume_select,
                    fresh_letter_field,
                    fresh_submit_btn,
                ) = await refetch_response_controls()
                if fresh_letter_field is not None:
                    return fresh_letter_field, fresh_submit_btn
                await self._page.wait_for_timeout(400)
            return None, None

        async def select_preferred_resume() -> bool:
            title_norm = _normalize_text(preferred_resume_title)
            id_norm = (preferred_resume_id or "").strip()

            if not resume_select and not title_norm and not id_norm:
                return True

            async def collect_resume_items():
                return await self._page.query_selector_all(
                    "[data-magritte-select-option], "
                    "[data-qa^='magritte-select-option-'], "
                    "[data-qa*='resume-item'], "
                    "[data-qa='vacancy-response-popup-form-resume'], "
                    "[data-qa='resume-select'] [role='button'][tabindex='0'], "
                    "[data-qa='resume-select'] [data-qa='cell'], "
                    "[data-qa='resume-select'] label, "
                    "label[data-qa='cell'], "
                    "[data-qa='resume-title'], "
                    "[data-qa='resume-detail'], "
                    "[data-qa='cell-text-content']"
                )

            async def has_resume_choices() -> bool:
                resume_items = await collect_resume_items()
                seen_texts = set()
                meaningful = 0
                for item in resume_items:
                    try:
                        text = _normalize_text(await item.inner_text())
                    except Exception:
                        continue
                    if not text or text in seen_texts:
                        continue
                    seen_texts.add(text)
                    if len(text) >= 6:
                        meaningful += 1
                    if meaningful >= 2:
                        return True
                return False

            async def expand_resume_picker():
                toggles = [
                    "[data-qa='resume-select'] [role='button'][tabindex='0']",
                    "[role='dialog'] [role='button'][tabindex='0']",
                    "form[name='vacancy_response'] [role='button'][tabindex='0']",
                    "[data-qa='vacancy-response-popup-form-resume']",
                    "[data-qa='resume-select'] [data-qa='cell']",
                    "[data-qa='resume-title']",
                    "[data-qa='resume-detail']",
                    "[data-qa='cell']",
                ]
                for selector in toggles:
                    handle = await self._page.query_selector(selector)
                    if not handle:
                        continue
                    if await self._click_with_fallbacks(handle, f"resume_toggle:{selector}"):
                        await self._page.wait_for_timeout(1000)
                        if await has_resume_choices():
                            return True
                return False

            if resume_select:
                await self._click_with_fallbacks(resume_select, "resume_select")
                await self._page.wait_for_timeout(1000)

            resume_items = await collect_resume_items()

            if not resume_items and (title_norm or id_norm):
                page_text = _normalize_text(
                    await self._page.evaluate("() => document.body.innerText.slice(0, 4000)")
                )
                if (title_norm and title_norm in page_text) or (id_norm and id_norm in page_text):
                    return True
                if not resume_select and (
                    letter_field is not None
                    or submit_btn is not None
                    or "/applicant/vacancy_response" in current_url
                ):
                    log.info(
                        "Resume picker is absent in hh apply flow — assuming current resume is already selected"
                    )
                    return True
                return False

            if not title_norm and not id_norm:
                if resume_items:
                    return await self._click_with_fallbacks(resume_items[0], "resume_item_default")
                return True

            async def find_matching_item(items):
                for item in items:
                    try:
                        text = _normalize_text(await item.inner_text())
                    except Exception:
                        continue
                    if not text:
                        continue
                    if id_norm and id_norm in text:
                        return item
                    if title_norm and (title_norm in text or text in title_norm):
                        return item
                return None

            best_item = await find_matching_item(resume_items)

            # Single-resume shortcut: если на странице ровно одно резюме
            # и пикер не раскрывается — считаем его выбранным
            if best_item is None and resume_items:
                unique_texts = set()
                for item in resume_items:
                    try:
                        text = _normalize_text(await item.inner_text())
                        if text and len(text) >= 6:
                            unique_texts.add(text)
                    except Exception:
                        continue
                if len(unique_texts) <= 1:
                    log.info("Single resume on page — treating as selected")
                    return True

            if best_item is None:
                expanded = await expand_resume_picker()
                if expanded:
                    resume_items = await collect_resume_items()
                    best_item = await find_matching_item(resume_items)

            if best_item is None:
                # Последняя попытка: проверить текст страницы
                page_text = _normalize_text(
                    await self._page.evaluate("() => document.body.innerText.slice(0, 6000)")
                )
                if title_norm and title_norm in page_text:
                    log.info("Resume title found in page text — treating as selected")
                    return True
                return False

            return await self._click_with_fallbacks(best_item, "resume_item_preferred")

        try:
            await self._page.goto(vacancy_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning("Vacancy page nav issue: %s", e)

        await self._page.wait_for_timeout(3000)
        await save_debug_snapshot("debug_apply_page")

        anti_bot_kind = await self._detect_anti_bot_kind()
        if anti_bot_kind:
            log.warning("hh.ru anti-bot (%s) encountered on vacancy page: %s", anti_bot_kind, self._page.url)
            if response_url and response_url != vacancy_url:
                log.info("Retrying apply flow via direct response URL: %s", response_url)
                try:
                    await self._page.goto(
                        response_url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception as e:
                    log.warning("Direct response page nav issue: %s", e)
                await self._page.wait_for_timeout(3000)
                await save_debug_snapshot("debug_apply_response_page")
        anti_bot_kind = await self._detect_anti_bot_kind()
        if anti_bot_kind:
            message = _anti_bot_message(anti_bot_kind, "на странице вакансии")
            self._remember_antibot_signal(anti_bot_kind, "vacancy_page", message)
            return {"ok": False, "message": message, "anti_bot_kind": anti_bot_kind}

        if await self._has_existing_response_ui():
            return await finalize_success("Уже откликались ранее", already_applied=True)

        # Ищем кнопку "Откликнуться" — собираем все data-qa для дебага
        apply_btn = await self._page.query_selector(
            "[data-qa='vacancy-response-link-top-again'], "
            "[data-qa='vacancy-response-link-bottom-again'], "
            "[data-qa='vacancy-response-link-top'], "
            "[data-qa='vacancy-response-link-bottom'], "
            "a[data-qa*='response-link'], "
            "button[data-qa*='vacancy-response']"
        )

        if not apply_btn:
            reapply_btn = await self._page.query_selector(
                "button:has-text('Отклик другим резюме'), "
                "a:has-text('Отклик другим резюме'), "
                "button:has-text('Откликнуться повторно'), "
                "a:has-text('Откликнуться повторно')"
            )
            if reapply_btn:
                return {"ok": True, "message": "Уже откликались ранее", "already_applied": True}

        if not apply_btn:
            # Попробуем найти по тексту
            apply_btn = await self._page.query_selector(
                "button:has-text('Откликнуться'), "
                "a:has-text('Откликнуться')"
            )

        if not apply_btn:
            # Возможно уже откликались
            if await self._has_existing_response_ui():
                return {"ok": True, "message": "Уже откликались ранее", "already_applied": True}

            (
                current_url,
                response_header,
                questions_required,
                resume_select,
                letter_field,
                submit_btn,
            ) = await detect_response_controls()
            direct_response_flow = (
                "/applicant/vacancy_response" in current_url
                or response_header is not None
                or resume_select is not None
                or letter_field is not None
                or submit_btn is not None
            )

            if questions_required:
                log.info("Vacancy requires employer questions — trying auto-answer")
                auto_question_result = await self._try_auto_answer_questions()
                auto_answer_notes.extend(auto_question_result.get("notes") or [])
                if auto_question_result.get("ok"):
                    return await finalize_success(
                        auto_question_result.get("message", "Отклик отправлен"),
                        notes=auto_answer_notes,
                    )
                return {
                    "ok": False,
                    "message": auto_question_result.get(
                        "message",
                        "Требуются доп. вопросы работодателя — пропускаем",
                    ),
                    "notes": auto_answer_notes,
                }

            if direct_response_flow:
                log.info("Direct response flow detected without initial vacancy button")
            else:
                # Дебаг: какие data-qa есть на странице
                qa_attrs = await self._page.evaluate(
                    "() => [...document.querySelectorAll('[data-qa]')].map(el => el.getAttribute('data-qa')).filter(a => a.includes('response') || a.includes('vacanc')).slice(0, 20)"
                )
                log.warning("Apply button not found. Relevant data-qa: %s", qa_attrs)
                return {"ok": False, "message": f"Кнопка не найдена. qa={qa_attrs[:5]}"}
        else:
            direct_response_flow = False

        if not direct_response_flow:
            log.info("Found apply button, clicking...")
            await apply_btn.scroll_into_view_if_needed()
            await self._page.wait_for_timeout(300)
            await apply_btn.click()
            await self._page.wait_for_timeout(3000)
            await save_debug_snapshot("debug_apply_after_click")

        if await self._apply_success_detected():
            return await finalize_success("Отклик отправлен")

        (
            current_url,
            response_header,
            questions_required,
            resume_select,
            letter_field,
            submit_btn,
        ) = await detect_response_controls()

        if questions_required:
            log.info("Vacancy requires employer questions — trying auto-answer")
            auto_question_result = await self._try_auto_answer_questions()
            auto_answer_notes.extend(auto_question_result.get("notes") or [])
            if auto_question_result.get("ok"):
                return await finalize_success(
                    auto_question_result.get("message", "Отклик отправлен"),
                    notes=auto_answer_notes,
                )
            return {
                "ok": False,
                "message": auto_question_result.get(
                    "message",
                    "Требуются доп. вопросы работодателя — пропускаем",
                ),
                "notes": auto_answer_notes,
            }

        if resume_select or preferred_resume_title or preferred_resume_id:
            log.info(
                "Selecting resume in hh apply flow (title=%r, id=%r)",
                preferred_resume_title,
                preferred_resume_id,
            )
            selected = await select_preferred_resume()
            if not selected:
                return {"ok": False, "message": "Не удалось выбрать нужное резюме"}
            await self._page.wait_for_timeout(1000)
            await self._dismiss_magritte_dropdowns()
            (
                current_url,
                response_header,
                questions_required,
                resume_select,
                letter_field,
                submit_btn,
            ) = await refetch_response_controls()

        if not letter_field and cover_letter:
            await self._expand_cover_letter_input()
            letter_field, refreshed_submit_btn = await refetch_letter_field()
            if refreshed_submit_btn is not None:
                submit_btn = refreshed_submit_btn

        if not letter_field:
            (
                _,
                _,
                _,
                _,
                letter_field,
                submit_btn,
            ) = await detect_response_controls()

        if letter_field and cover_letter:
            log.info("Filling cover letter...")
            for attempt in range(2):
                try:
                    await letter_field.scroll_into_view_if_needed()
                    await self._page.wait_for_timeout(300)
                    await letter_field.fill("")
                    await letter_field.type(cover_letter, delay=20)
                    break
                except Exception as e:
                    if "not attached to the DOM" not in str(e):
                        raise
                    log.warning("Cover letter field detached from DOM, refetching controls (attempt %d)", attempt + 1)
                    letter_field, refreshed_submit_btn = await refetch_letter_field()
                    if refreshed_submit_btn is not None:
                        submit_btn = refreshed_submit_btn
                    if not letter_field:
                        raise
            await self._page.wait_for_timeout(500)
            cover_letter_filled = True
            await self._dismiss_magritte_dropdowns()

        if submit_btn:
            (
                _,
                _,
                _,
                _,
                _,
                refreshed_submit_btn,
            ) = await refetch_response_controls()
            if refreshed_submit_btn is not None:
                submit_btn = refreshed_submit_btn
            await self._dismiss_magritte_dropdowns()
            clicked = await self._click_with_fallbacks(submit_btn, "submit_button")
            if not clicked:
                clicked = await self._submit_response_form_via_dom()
            if not clicked:
                return {"ok": False, "message": "Не удалось нажать кнопку подтверждения"}
            await self._page.wait_for_timeout(4000)

        await save_debug_snapshot("debug_apply_after_submit")

        anti_bot_kind = await self._detect_anti_bot_kind()
        if anti_bot_kind:
            message = _anti_bot_message(anti_bot_kind, "после отклика")
            log.warning("HH anti-bot (%s) appeared after apply submit", anti_bot_kind)
            self._remember_antibot_signal(anti_bot_kind, "apply_submit", message)
            return {"ok": False, "message": message, "anti_bot_kind": anti_bot_kind}

        if await self._response_requires_questions():
            log.info("Vacancy requires employer questions after submit — trying auto-answer")
            auto_question_result = await self._try_auto_answer_questions()
            auto_answer_notes.extend(auto_question_result.get("notes") or [])
            if auto_question_result.get("ok"):
                return await finalize_success(
                    auto_question_result.get("message", "Отклик отправлен"),
                    notes=auto_answer_notes,
                )
            return {
                "ok": False,
                "message": auto_question_result.get(
                    "message",
                    "Требуются доп. вопросы работодателя — пропускаем",
                ),
                "notes": auto_answer_notes,
            }

        if await self._apply_success_detected():
            return await finalize_success("Отклик отправлен", notes=auto_answer_notes)

        (
            current_url,
            response_header,
            questions_required,
            _,
            _,
            submit_btn_retry,
        ) = await detect_response_controls()
        if response_header is not None and not questions_required and submit_btn_retry is not None:
            log.info("Retrying hh submit after inconclusive response state")
            await self._dismiss_magritte_dropdowns()
            retried = await self._submit_response_form_via_dom()
            if retried:
                await self._page.wait_for_timeout(4000)
                anti_bot_kind = await self._detect_anti_bot_kind()
                if anti_bot_kind:
                    message = _anti_bot_message(anti_bot_kind, "после отклика")
                    log.warning("HH anti-bot (%s) appeared after DOM submit fallback", anti_bot_kind)
                    self._remember_antibot_signal(anti_bot_kind, "apply_submit_retry", message)
                    return {"ok": False, "message": message, "anti_bot_kind": anti_bot_kind}
                if await self._response_requires_questions():
                    log.info("Vacancy requires employer questions after retry — trying auto-answer")
                    auto_question_result = await self._try_auto_answer_questions()
                    auto_answer_notes.extend(auto_question_result.get("notes") or [])
                    if auto_question_result.get("ok"):
                        return await finalize_success(
                            auto_question_result.get("message", "Отклик отправлен"),
                            notes=auto_answer_notes,
                        )
                    return {
                        "ok": False,
                        "message": auto_question_result.get(
                            "message",
                            "Требуются доп. вопросы работодателя — пропускаем",
                        ),
                        "notes": auto_answer_notes,
                    }
                if await self._apply_success_detected():
                    return await finalize_success("Отклик отправлен", notes=auto_answer_notes)

        page_text = await self._page_text(limit=20000)
        page_lower = page_text.lower()

        # Ошибка rate limit / блокировки
        if "слишком много" in page_lower or "too many" in page_lower:
            log.warning("Rate limit detected after apply")
            message = _anti_bot_message("rate_limit")
            self._remember_antibot_signal("rate_limit", "apply_verify", message)
            return {"ok": False, "message": message, "anti_bot_kind": "rate_limit"}

        # Ошибка на стороне hh
        if "что-то пошло не так" in page_lower or "произошла ошибка" in page_lower or "ошибка" in page_lower:
            log.warning("hh.ru error page after apply. URL: %s", self._page.url)
            return {"ok": False, "message": "hh.ru показал ошибку после отклика"}

        log.warning("Apply verification failed. URL: %s", self._page.url)
        await save_debug_snapshot("debug_apply_verification_failed")
        return {"ok": False, "message": "Не удалось подтвердить отклик"}

    async def _fill_cover_letter_post_apply(self, cover_letter: str):
        """Заполнить сопроводительное письмо на странице после успешного отклика."""
        try:
            letter_selectors = (
                "textarea[placeholder*='Сопроводительное']",
                "textarea[placeholder*='сопроводительное']",
                "textarea[placeholder*='Сообщение']",
                "textarea[placeholder*='сообщение']",
                "textarea[name='letter']",
                "textarea",
                "input[placeholder*='Сообщение']",
                "[contenteditable='true'][role='textbox']",
                "[contenteditable='true']",
            )
            send_selectors = (
                "button:has-text('Отправить')",
                "[data-qa*='send']",
                "[type='submit']",
            )

            surfaces = [self._page]
            page_frames = getattr(self._page, "frames", None)
            if page_frames:
                surfaces.extend(frame for frame in page_frames if frame is not self._page.main_frame)

            snippet = _normalize_text(cover_letter[:120])
            await self._expand_cover_letter_input()
            for surface in surfaces:
                try:
                    surface_text = await surface.evaluate(
                        "() => document.body ? document.body.innerText.slice(0, 12000) : ''"
                    )
                except Exception:
                    surface_text = ""
                if snippet and snippet in _normalize_text(surface_text):
                    log.info("Cover letter already visible after apply; skipping duplicate send")
                    return

                for selector in letter_selectors:
                    try:
                        letter_field = await surface.query_selector(selector)
                    except Exception:
                        continue
                    if not letter_field:
                        continue

                    try:
                        await letter_field.scroll_into_view_if_needed()
                    except Exception:
                        pass

                    try:
                        await letter_field.click()
                    except Exception:
                        pass

                    await self._page.wait_for_timeout(300)

                    filled = False
                    try:
                        await letter_field.fill("")
                        await letter_field.type(cover_letter, delay=20)
                        filled = True
                    except Exception:
                        try:
                            await letter_field.evaluate(
                                """(el, value) => {
                                    el.focus();
                                    if ('value' in el) {
                                        el.value = '';
                                        el.dispatchEvent(new Event('input', { bubbles: true }));
                                        el.value = value;
                                        el.dispatchEvent(new Event('input', { bubbles: true }));
                                        el.dispatchEvent(new Event('change', { bubbles: true }));
                                        return;
                                    }
                                    if (el.isContentEditable) {
                                        el.textContent = value;
                                        el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value }));
                                    }
                                }""",
                                cover_letter,
                            )
                            filled = True
                        except Exception:
                            filled = False

                    if not filled:
                        continue

                    await self._page.wait_for_timeout(500)

                    sent = False
                    for send_selector in send_selectors:
                        try:
                            send_btn = await surface.query_selector(send_selector)
                        except Exception:
                            continue
                        if not send_btn:
                            continue
                        try:
                            await send_btn.scroll_into_view_if_needed()
                        except Exception:
                            pass
                        try:
                            await send_btn.click()
                            await self._page.wait_for_timeout(2000)
                            sent = True
                            break
                        except Exception:
                            continue

                    if not sent:
                        try:
                            await letter_field.press("Enter")
                            await self._page.wait_for_timeout(2000)
                            sent = True
                        except Exception:
                            pass

                    if sent:
                        log.info("Cover letter sent after apply")
                        return

            log.debug("No cover letter field found after apply")
        except Exception as e:
            log.warning("Failed to fill cover letter post-apply: %s", e)

    # ── Проверка откликов / приглашений ───────────────────────────────────

    async def check_negotiations(self) -> dict:
        """
        Проверить статус откликов.
        Возвращает {"invitations": [...], "responses": int, "new_messages": int}
        """
        await self._page.goto(
            f"{config.HH_BASE_URL}/applicant/negotiations",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await self._page.wait_for_timeout(3000)

        result = {"invitations": [], "responses": 0, "new_messages": 0}

        # Считаем общее количество откликов
        try:
            tabs = await self._page.query_selector_all("[data-qa*='negotiations__tab']")
            for tab in tabs:
                text = (await tab.inner_text()).strip().lower()
                # Вытаскиваем число из текста вкладки
                import re
                nums = re.findall(r"\d+", text)
                if "приглашен" in text and nums:
                    # Вкладка приглашений
                    pass
        except Exception:
            pass

        # Кликаем на вкладку "Приглашения"
        invite_tab = await self._page.query_selector(
            "[data-qa='negotiations__tab_invitation'], "
            "a[href*='invitation']"
        )
        if invite_tab:
            await invite_tab.click()
            await self._page.wait_for_timeout(2000)

            invite_cards = await self._page.query_selector_all(
                "[data-qa='negotiations-item'], "
                ".negotiations-item, "
                ".resume-negotiations-item"
            )
            for card in invite_cards:
                try:
                    title_el = await card.query_selector("a[data-qa*='title'], h3 a, a")
                    if title_el:
                        title = (await title_el.inner_text()).strip()
                        href = await title_el.get_attribute("href") or ""
                        company_el = await card.query_selector(
                            "[data-qa*='employer'], .negotiations-item__company"
                        )
                        company = (await company_el.inner_text()).strip() if company_el else "—"
                        result["invitations"].append({
                            "title": title,
                            "company": company,
                            "url": href,
                        })
                except Exception:
                    pass

        return result

    async def get_negotiation_statuses(self) -> list[dict]:
        """Прочитать видимые статусы откликов на странице переговоров."""
        await self._page.goto(
            f"{config.HH_BASE_URL}/applicant/negotiations",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await self._page.wait_for_timeout(3000)

        items = []
        cards = await self._page.query_selector_all(
            "[data-qa='negotiations-item'], "
            ".negotiations-item, "
            ".resume-negotiations-item"
        )
        for card in cards:
            try:
                text = (await card.inner_text()).strip()
                if not text:
                    continue
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                if len(lines) < 3:
                    continue
                link = await card.query_selector("a[href*='/vacancy/']")
                href = await link.get_attribute("href") if link else ""
                href = _absolute_hh_url(href or "")
                vacancy_id = ""
                if "/vacancy/" in href:
                    vacancy_id = href.split("/vacancy/")[-1].split("?")[0].split("/")[0]
                items.append(
                    {
                        "id": vacancy_id,
                        "status": lines[0],
                        "title": lines[1],
                        "company": lines[2],
                        "url": href,
                    }
                )
            except Exception:
                pass
        return items

    # ── Получить список резюме ────────────────────────────────────────────

    async def get_resume_ids(self) -> list[dict]:
        """Получить ID резюме пользователя."""
        try:
            await self._page.goto(
                f"{config.HH_BASE_URL}/applicant/resumes",
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as e:
            log.warning("Resume page navigation issue: %s", e)

        await self._page.wait_for_timeout(4000)

        # Дебаг: скриншот и URL
        current_url = self._page.url
        log.info("Resume page URL: %s", current_url)
        debug_screenshot = os.path.join(config.HH_STATE_DIR, "debug_resumes.png")
        debug_html = os.path.join(config.HH_STATE_DIR, "debug_resumes.html")
        try:
            await self._page.screenshot(path=debug_screenshot)
            html = await self._page.content()
            with open(debug_html, "w") as f:
                f.write(html)
            log.info("Debug saved: %s, %s", debug_screenshot, debug_html)
        except Exception as e:
            log.debug("Debug save failed: %s", e)

        anti_bot_kind = await self._detect_anti_bot_kind()
        if anti_bot_kind:
            message = _anti_bot_message(anti_bot_kind, "на странице резюме")
            self._remember_antibot_signal(anti_bot_kind, "resume_page", message)
            log.warning("hh.ru anti-bot (%s) on resume page: %s", anti_bot_kind, self._page.url)
            return []

        resumes = []

        # Стратегия 1: data-qa селекторы
        cards = await self._page.query_selector_all("[data-qa='resume']")
        log.info("Strategy 1 (data-qa='resume'): %d cards", len(cards))

        # Стратегия 2: ссылки с /resume/ в href
        if not cards:
            cards = await self._page.query_selector_all("a[href*='/resume/']")
            log.info("Strategy 2 (a[href*='/resume/']): %d links", len(cards))
            seen_ids = set()
            for link in cards:
                href = await link.get_attribute("href") or ""
                if "/resume/" not in href:
                    continue
                resume_id = href.split("/resume/")[-1].split("?")[0].split("/")[0]
                if not resume_id or resume_id in seen_ids:
                    continue
                seen_ids.add(resume_id)
                title = (await link.inner_text()).strip() or resume_id
                if not title or len(title) > 200:
                    title = resume_id
                resumes.append({"id": resume_id, "title": title, "url": href})
            return resumes

        # Стратегия 1 продолжение: парсим карточки
        for card in cards:
            title_el = await card.query_selector(
                "[data-qa='resume-title'], "
                "a[data-qa*='title'], "
                "a[href*='/resume/']"
            )
            if title_el:
                title = (await title_el.inner_text()).strip()
                href = await title_el.get_attribute("href") or ""
                resume_id = ""
                if "/resume/" in href:
                    resume_id = href.split("/resume/")[-1].split("?")[0].split("/")[0]
                resumes.append({"id": resume_id, "title": title, "url": href})

        return resumes

    # ── Скачать полное резюме ─────────────────────────────────────────────

    async def download_resume(self) -> dict:
        """Скачать первое резюме. Для выбора используй download_resume_by_id."""
        resumes = await self.get_resume_ids()
        if not resumes:
            log.error("No resumes found on listing page")
            log.error("Check debug: %s/debug_resumes.png", config.HH_STATE_DIR)
            return {"title": "", "sections": {}, "raw": ""}
        return await self.download_resume_by_id(resumes[0])

    async def download_resume_by_id(self, resume: dict) -> dict:
        """
        Скачать конкретное резюме.
        resume: {"id": str, "title": str, "url": str}
        Возвращает {"title": str, "sections": {name: text}, "raw": str}
        """
        resume_url = resume.get("url", "")
        if not resume_url:
            resume_url = f"{config.HH_BASE_URL}/resume/{resume['id']}"
        if not resume_url.startswith("http"):
            resume_url = f"{config.HH_BASE_URL}{resume_url}"

        log.info("Downloading resume: %s (%s)", resume["title"], resume_url)

        try:
            await self._page.goto(resume_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.warning("Resume page nav issue: %s", e)

        await self._page.wait_for_timeout(4000)

        # Скроллим вниз чтобы подгрузить lazy-loaded блоки
        await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await self._page.wait_for_timeout(2000)
        await self._page.evaluate("window.scrollTo(0, 0)")
        await self._page.wait_for_timeout(1000)

        # Дебаг скриншот страницы резюме
        try:
            debug_path = os.path.join(config.HH_STATE_DIR, "debug_resume_page.png")
            await self._page.screenshot(path=debug_path, full_page=True)
            debug_html = os.path.join(config.HH_STATE_DIR, "debug_resume_page.html")
            html = await self._page.content()
            with open(debug_html, "w") as f:
                f.write(html)
            log.info("Resume page debug saved: %s", debug_path)
        except Exception:
            pass

        sections = {}

        # Заголовок (должность)
        title_el = await self._page.query_selector("[data-qa='resume-block-title-position']")
        title = (await title_el.inner_text()).strip() if title_el else resume["title"]

        # Зарплатные ожидания
        salary_el = await self._page.query_selector("[data-qa='resume-block-salary']")
        if salary_el:
            sections["Зарплата"] = (await salary_el.inner_text()).strip()

        # Позиция (формат работы, занятость, командировки)
        position_card = await self._page.query_selector("[data-qa='resume-position-card']")
        if position_card:
            pos_text = (await position_card.inner_text()).strip()
            if pos_text:
                sections["Позиция"] = pos_text

        # Опыт работы
        exp_card = await self._page.query_selector("[data-qa='resume-list-card-experience']")
        if exp_card:
            exp_text = (await exp_card.inner_text()).strip()
            if exp_text:
                sections["Опыт работы"] = exp_text

        # Навыки (карточка)
        skills_card = await self._page.query_selector("[data-qa='skills-card']")
        if skills_card:
            skills_text = (await skills_card.inner_text()).strip()
            if skills_text:
                sections["Навыки"] = skills_text

        # Подтверждённые навыки / методы
        skills_methods = await self._page.query_selector("[data-qa='skills-methods']")
        if skills_methods:
            sm_text = (await skills_methods.inner_text()).strip()
            if sm_text:
                sections["Подтверждение навыков"] = sm_text

        # Образование
        edu_card = await self._page.query_selector("[data-qa='resume-list-card-education']")
        if edu_card:
            edu_text = (await edu_card.inner_text()).strip()
            if edu_text:
                sections["Образование"] = edu_text

        # О себе
        about_card = await self._page.query_selector("[data-qa='resume-about-card']")
        if about_card:
            about_text = (await about_card.inner_text()).strip()
            if about_text:
                sections["О себе"] = about_text

        # Fallback: если мало секций — парсим все карточки на странице
        if len(sections) < 3:
            log.info("Few sections found (%d), trying fallback parser", len(sections))
            all_cards = await self._page.query_selector_all("[data-qa$='-card']")
            for card in all_cards:
                qa = await card.get_attribute("data-qa") or ""
                if qa in ("resume-position-card", "resume-about-card",
                          "skills-card", "resume-visibility-card",
                          "resume-list-card-experience",
                          "resume-list-card-education"):
                    continue  # уже обработали или не нужно
                text = (await card.inner_text()).strip()
                if text and len(text) > 20:
                    name = qa.replace("resume-", "").replace("-card", "").replace("-", " ").title()
                    sections[name] = text

        # Собираем в markdown
        raw = f"# {title}\n\n"
        for name, text in sections.items():
            raw += f"## {name}\n{text}\n\n"

        log.info("Downloaded resume: %s (%d sections)", title, len(sections))
        return {"title": title, "sections": sections, "raw": raw}
