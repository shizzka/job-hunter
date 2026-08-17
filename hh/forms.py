"""Pure helpers for HH employer questionnaires."""

import re

from hh.text import normalize_text


RISKY_QUESTION_PATTERNS = [
    r"коммерческ\w*\s+опыт\w*.{0,80}(aqa|автотест|automation|selenium|java|playwright|cypress)",
    r"(aqa|automation|selenium|java|playwright|cypress).{0,80}коммерческ\w*\s+опыт",
    r"сколько\s+лет.{0,80}(aqa|автотест|automation|selenium|java|playwright|cypress)",
]

STABLE_ANSWER_LIBRARY = [
    (
        (r"\bapi\b", r"rest|postman|swagger|json|http"),
        "Есть практический опыт REST API: проверяю запросы и ответы в Postman/DevTools, смотрю JSON, статусы, негативные сценарии и связь API с пользовательским поведением.",
    ),
    (
        (r"postman|swagger|openapi",),
        "Работал с Postman и Swagger/OpenAPI на уровне ручной проверки API: запросы, параметры, JSON-ответы, статусы и базовые негативные сценарии.",
    ),
    (
        (r"\bsql\b|баз\w*\s+данн|select|join",),
        "SQL на базовом уровне: SELECT-запросы, фильтрация, простые JOIN и проверка данных для тестовых сценариев.",
    ),
    (
        (r"тестов\w*\s+документац|тест[-\s]?кейс|чек[-\s]?лист|баг[-\s]?репорт|test\s?case|bug\s?report",),
        "Веду тестовую документацию: тест-кейсы, чек-листы и баг-репорты. В баге фиксирую шаги, фактический/ожидаемый результат, окружение и вложения.",
    ),
    (
        (r"автотест|pytest|python|aqa|automation",),
        "Участвовал в разработке API-автотестов на Python/pytest: помогал со сценариями, покрытием, окружением и запуском готовых тестов. Основной профиль сейчас - manual/API QA, без позиционирования как самостоятельный AQA.",
    ),
    (
        (r"тестов\w*\s+задан|тестовое|test\s+task",),
        "Готов выполнить тестовое задание, если оно разумное по объему и связано с задачами вакансии.",
    ),
    (
        (r"формат\s+работ|удален|удалён|remote|офис|гибрид|график",),
        "Готов обсуждать формат работы. В приоритете удаленный или гибридный формат, детали зависят от задач, графика и команды.",
    ),
]


def extract_resume_salary_text(resume_text: str) -> str:
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


def extract_numeric_salary(value: str) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if not digits:
        return ""
    if len(digits) > 6:
        digits = digits[:6]
    return digits


def is_salary_question(value: str) -> bool:
    text = normalize_text(value)
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


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def format_question_answer_note(question: str, answer: str, *, control: str = "") -> str:
    label = truncate_text((question or "вопрос").strip(), 120)
    value = truncate_text((answer or "—").strip(), 220)
    prefix = f"автоответ hh ({control}): " if control else "автоответ hh: "
    return f"{prefix}{label} -> {value}"


def question_answer_item(
    question: str,
    answer: str,
    *,
    control: str = "",
    best_guess: bool = False,
    required: bool = False,
    starred: bool = False,
    skipped: bool = False,
    skip_reason: str = "",
) -> dict:
    item = {
        "question": truncate_text((question or "вопрос").strip(), 500),
        "answer": truncate_text((answer or "—").strip(), 1000),
    }
    if control:
        item["control"] = control
    if best_guess:
        item["best_guess"] = True
    if required:
        item["required"] = True
    if starred:
        item["starred"] = True
    if skipped:
        item["skipped"] = True
    if skip_reason:
        item["skip_reason"] = truncate_text(skip_reason, 200)
    return item


def is_risky_question(question_text: str) -> bool:
    text = normalize_text(question_text)
    return any(re.search(pattern, text) for pattern in RISKY_QUESTION_PATTERNS)


def answer_question_from_library(question_text: str, *, max_chars: int) -> str | None:
    text = normalize_text(question_text)
    if not text or is_risky_question(text):
        return None
    for patterns, answer in STABLE_ANSWER_LIBRARY:
        if all(re.search(pattern, text) for pattern in patterns):
            return truncate_text(answer, max_chars)
    return None


async def inspect_employer_questions(page, *, logger) -> dict:
    try:
        result = await page.evaluate(
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
                const seenGroups = new Set();
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

                const findNearbyTaskQuestion = (el) => {
                    let node = el;
                    for (let depth = 0; node && depth < 7; depth += 1) {
                        const parent = node.parentElement;
                        if (!parent) break;

                        const scoped = parent.querySelector('[data-qa="task-question"]');
                        if (scoped) {
                            const text = clean(scoped.innerText);
                            if (text) return text;
                        }

                        let next = node.nextElementSibling;
                        for (let idx = 0; next && idx < 4; idx += 1) {
                            const candidate = next.matches?.('[data-qa="task-question"]')
                                ? next
                                : next.querySelector?.('[data-qa="task-question"]');
                            const text = clean(candidate?.innerText || "");
                            if (text) return text;
                            next = next.nextElementSibling;
                        }

                        node = parent;
                    }
                    return "";
                };

                const describeField = (el) => {
                    const labels = el.labels ? Array.from(el.labels).map((label) => clean(label.innerText)).filter(Boolean) : [];
                    const promptParts = collectPromptTexts(el);
                    const placeholder = clean(el.getAttribute("placeholder") || "");
                    const taskQuestion = findNearbyTaskQuestion(el);
                    const fallbackQuestionText = clean(
                        [labels.join(" "), ...promptParts]
                            .filter(Boolean)
                            .join(" ")
                    );
                    const questionText = clean(taskQuestion || fallbackQuestionText).slice(0, 500);

                    return {
                        question_text: questionText,
                        placeholder,
                    };
                };

                const optionLabel = (input) => {
                    const lbl = input.closest("label");
                    if (lbl) {
                        // collect text but skip the input's own value
                        const clone = lbl.cloneNode(true);
                        clone.querySelectorAll("input, textarea, select").forEach((node) => node.remove());
                        const text = clean(clone.innerText);
                        if (text) return text;
                    }
                    // fallback: associated label[for=id]
                    if (input.id) {
                        const ext = document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
                        if (ext) {
                            const t = clean(ext.innerText);
                            if (t) return t;
                        }
                    }
                    // fallback: aria-label or value attribute
                    return clean(input.getAttribute("aria-label") || input.value || "");
                };

                const radioGroupQuestionText = (anyInput) => {
                    const taskQuestion = findNearbyTaskQuestion(anyInput);
                    if (taskQuestion) return taskQuestion.slice(0, 500);

                    const fieldset = anyInput.closest("fieldset");
                    if (fieldset) {
                        const legend = fieldset.querySelector("legend");
                        if (legend) {
                            const t = clean(legend.innerText);
                            if (t) return t;
                        }
                    }
                    // fallback to standard prompt collector on the first input
                    const parts = collectPromptTexts(anyInput);
                    return clean(parts.join(" ")).slice(0, 500);
                };

                const isCustomOption = (text) => {
                    const lc = (text || "").toLowerCase();
                    return lc.includes("свой вариант") || lc.includes("другое") || lc.includes("своя версия");
                };

                const isRequired = (el, members = []) => {
                    const all = members.length ? members : [el];
                    return all.some((node) =>
                        node.required ||
                        node.getAttribute("aria-required") === "true" ||
                        (node.closest("[aria-required='true']") !== null)
                    );
                };

                const isStarred = (text) => new RegExp("(^|\\s)\\*($|\\s)|★|обязател", "i").test(text || "");

                for (const el of nodes) {
                    const tag = (el.tagName || "").toLowerCase();
                    const inputType = tag === "input"
                        ? ((el.getAttribute("type") || "text").toLowerCase())
                        : tag;

                    // ---- radio / checkbox groups ----
                    if (inputType === "radio" || inputType === "checkbox") {
                        const groupName = el.getAttribute("name") || "";
                        const groupKey = `${inputType}:${groupName || el.id || (`__nameless_${seq}`)}`;
                        if (seenGroups.has(groupKey)) continue;
                        seenGroups.add(groupKey);

                        let members;
                        if (groupName) {
                            members = Array.from(
                                document.querySelectorAll(`input[type="${inputType}"][name="${CSS.escape(groupName)}"]`)
                            ).filter(visible);
                        } else {
                            members = [el];
                        }
                        if (!members.length) continue;

                        const autoId = el.getAttribute("data-codex-auto-field-id") || `codex-auto-field-${++seq}`;
                        // anchor id stored on first input of the group
                        members[0].setAttribute("data-codex-auto-field-id", autoId);

                        const options = members.map((m, idx) => {
                            const label = optionLabel(m);
                            return {
                                index: idx,
                                value: m.value || "",
                                label: label.slice(0, 200),
                                is_custom: isCustomOption(label),
                            };
                        });

                        fields.push({
                            field_id: autoId,
                            control: inputType,            // "radio" | "checkbox"
                            input_type: inputType,
                            group_name: groupName,
                            question_text: radioGroupQuestionText(el).slice(0, 500),
                            placeholder: "",
                            options: options.slice(0, 12),
                            max_length: 0,
                            required: isRequired(el, members),
                            starred: isStarred(radioGroupQuestionText(el)),
                        });
                        continue;
                    }

                    // ---- select ----
                    if (tag === "select") {
                        const groupKey = `select:${el.getAttribute("name") || el.id || seq}`;
                        if (seenGroups.has(groupKey)) continue;
                        seenGroups.add(groupKey);

                        const autoId = el.getAttribute("data-codex-auto-field-id") || `codex-auto-field-${++seq}`;
                        el.setAttribute("data-codex-auto-field-id", autoId);

                        const opts = Array.from(el.querySelectorAll("option"))
                            .filter((o) => !o.disabled)
                            .map((o, idx) => ({
                                index: idx,
                                value: o.value || "",
                                label: clean(o.innerText || o.value).slice(0, 200),
                                is_custom: isCustomOption(o.innerText),
                            }))
                            .filter((o) => o.label && o.value !== ""); // drop placeholder "выберите"

                        const described = describeField(el);
                        fields.push({
                            field_id: autoId,
                            control: "select",
                            input_type: "select",
                            group_name: el.getAttribute("name") || "",
                            question_text: described.question_text,
                            placeholder: described.placeholder,
                            options: opts.slice(0, 12),
                            max_length: 0,
                            required: isRequired(el),
                            starred: isStarred(described.question_text),
                        });
                        continue;
                    }

                    // ---- text / textarea / number / etc ----
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
                        required: isRequired(el),
                        starred: isStarred(described.question_text),
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
        logger.warning("Question form inspection failed: %s", exc)
        return {"page_text": "", "fields": [], "unsupported_fields": 0, "unsupported_items": []}

    if not isinstance(result, dict):
        return {"page_text": "", "fields": [], "unsupported_fields": 0, "unsupported_items": []}
    result.setdefault("page_text", "")
    result.setdefault("fields", [])
    result.setdefault("unsupported_fields", 0)
    result.setdefault("unsupported_items", [])
    return result


async def fill_employer_question_answers(page, answers: list[dict], *, logger) -> dict:
    try:
        return await page.evaluate(
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

                const findGroupMembers = (anchor, control) => {
                    const name = anchor.getAttribute("name");
                    if (!name) return [anchor];
                    const sel = `input[type="${control}"][name="${CSS.escape(name)}"]`;
                    return Array.from(document.querySelectorAll(sel));
                };

                // Find the "custom-text" companion input near a "Свой вариант" radio.
                // Heuristic: look in the radio's <label> for textarea/input, then in the
                // closest fieldset / parent block for an unbound text input that
                // appears AFTER the radio.
                const findCustomTextNear = (radio) => {
                    const label = radio.closest("label");
                    if (label) {
                        const inner = label.querySelector("input[type='text'], textarea");
                        if (inner) return inner;
                    }
                    const parent = radio.closest("fieldset") || radio.parentElement?.parentElement;
                    if (!parent) return null;
                    const candidates = Array.from(parent.querySelectorAll("input[type='text'], textarea"));
                    // pick first that is NOT a radio's label-embedded text input of another option
                    for (const c of candidates) {
                        if (c.disabled) continue;
                        // skip if it is in a *different* option's label
                        const cLabel = c.closest("label");
                        if (cLabel && cLabel.querySelector("input[type='radio'], input[type='checkbox']")) {
                            // text input INSIDE a label that wraps a radio — accept only if that radio is `radio`
                            const ownerRadio = cLabel.querySelector("input[type='radio'], input[type='checkbox']");
                            if (ownerRadio === radio) return c;
                            continue;
                        }
                        return c;
                    }
                    return null;
                };

                const clickOption = (input) => {
                    try {
                        input.focus();
                    } catch (e) {}
                    if (!input.checked) {
                        input.click();
                        dispatch(input, "input");
                        dispatch(input, "change");
                    }
                };

                const result = { filled: 0, errors: [] };
                for (const item of plan || []) {
                    const selector = `[data-codex-auto-field-id="${item.field_id}"]`;
                    const anchor = document.querySelector(selector);
                    if (!anchor) {
                        result.errors.push(`field ${item.field_id} not found`);
                        continue;
                    }
                    try {
                        const control = (item.control || "").toLowerCase();
                        if (control === "radio" || control === "checkbox") {
                            const members = findGroupMembers(anchor, control);
                            if (!members.length) {
                                result.errors.push(`field ${item.field_id} no members`);
                                continue;
                            }
                            const selIndices = Array.isArray(item.selected_indices) ? item.selected_indices : [];
                            if (!selIndices.length) {
                                result.errors.push(`field ${item.field_id} no selection`);
                                continue;
                            }
                            // For radio: uncheck not needed, native; for checkbox: clear others if exclusive flag?
                            // We follow "select only what LLM picked" — uncheck members not in selIndices.
                            if (control === "checkbox") {
                                members.forEach((m, idx) => {
                                    const wantChecked = selIndices.includes(idx);
                                    if (m.checked !== wantChecked) {
                                        m.click();
                                        dispatch(m, "input");
                                        dispatch(m, "change");
                                    }
                                });
                            } else {
                                const idx = selIndices[0];
                                if (idx < 0 || idx >= members.length) {
                                    result.errors.push(`field ${item.field_id} index ${idx} out of range`);
                                    continue;
                                }
                                clickOption(members[idx]);
                            }
                            // If LLM provided custom_text and the chosen option is "Свой вариант"-style,
                            // fill the companion text input.
                            if (item.custom_text) {
                                const idx = selIndices[0];
                                const ownerRadio = members[idx] || anchor;
                                const txt = findCustomTextNear(ownerRadio);
                                if (txt) {
                                    txt.focus();
                                    setValue(txt, String(item.custom_text));
                                }
                            }
                            result.filled += 1;
                        } else if (control === "select") {
                            const selIndices = Array.isArray(item.selected_indices) ? item.selected_indices : [];
                            if (!selIndices.length) {
                                result.errors.push(`field ${item.field_id} no selection`);
                                continue;
                            }
                            const opts = Array.from(anchor.querySelectorAll("option"));
                            const idx = selIndices[0];
                            if (idx < 0 || idx >= opts.length) {
                                result.errors.push(`field ${item.field_id} select index ${idx} out of range`);
                                continue;
                            }
                            anchor.focus();
                            anchor.value = opts[idx].value;
                            dispatch(anchor, "input");
                            dispatch(anchor, "change");
                            result.filled += 1;
                        } else {
                            anchor.focus();
                            setValue(anchor, String(item.answer ?? ""));
                            result.filled += 1;
                        }
                    } catch (err) {
                        result.errors.push(String(err));
                    }
                }
                return result;
            }""",
            answers,
        )
    except Exception as exc:
        logger.warning("Question form fill failed: %s", exc)
        return {"filled": 0, "errors": [str(exc)]}


async def submit_employer_questions(
    page,
    *,
    submit_response_form_via_dom,
    click_with_fallbacks,
) -> bool:
    if await submit_response_form_via_dom():
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
            button = await page.query_selector(selector)
        except Exception:
            continue
        if button and await click_with_fallbacks(button, f"question_submit:{selector}"):
            return True
    return False


async def answer_question_with_llm(
    field: dict,
    resume_text: str,
    page_text: str = "",
    vacancy_context: str = "",
    *,
    settings,
    logger,
    get_question_answer_client,
    build_salary_rule_block,
    build_facts_block,
    build_profile_note_block,
    build_filtered_kb_block,
    build_knowledge_base_block,
    parse_llm_json,
    repair_llm_json,
) -> str | None:
    if not settings.HH_AUTO_ANSWER_USE_LLM or not settings.LLM_API_KEY or not resume_text.strip():
        return None

    question_text = (field.get("question_text") or field.get("placeholder") or "").strip()
    if not question_text:
        return None

    field_type = (field.get("input_type") or field.get("control") or "text").strip() or "text"
    max_chars = settings.HH_AUTO_ANSWER_MAX_CHARS
    field_max_length = int(field.get("max_length") or 0)
    if field_max_length > 0:
        max_chars = min(max_chars, field_max_length)

    if is_risky_question(question_text):
        logger.info("HH auto-answer skipped risky question: %s", truncate_text(question_text, 120))
        return None

    stable_answer = answer_question_from_library(question_text, max_chars=max_chars)
    if stable_answer:
        return stable_answer

    vacancy_block = (
        f"Контекст вакансии (на неё откликаемся):\n{truncate_text(vacancy_context, 1500)}\n\n"
        if vacancy_context else ""
    )
    salary_block = build_salary_rule_block()
    facts_block = build_facts_block()
    profile_note_block = build_profile_note_block()
    # 2-pass: фильтруем KB под конкретную вакансию (если контекст есть)
    try:
        knowledge_block = await build_filtered_kb_block(
            vacancy_context, get_question_answer_client(),
            max_sections=5, limit_chars=8000,
        )
    except Exception as exc:
        logger.debug("filtered KB failed, fallback to full: %s", exc)
        knowledge_block = build_knowledge_base_block(limit_chars=8000)

    prompt = f"""Ты отвечаешь на вопрос работодателя на hh.ru от имени кандидата.

Опирайся на канонический профиль (приоритет), структурированные факты, факты из резюме и контекст вакансии. Ничего не выдумывай.
Если ни в фактах ни в резюме нельзя ответить уверенно — верни status=skip.

Тип поля: {field_type}
Максимум символов: {max_chars}
Вопрос: {question_text}
Контекст формы: {truncate_text(page_text, 1200) if page_text else "(нет)"}

{profile_note_block}{knowledge_block}{salary_block}{facts_block}{vacancy_block}Резюме кандидата:
{resume_text[:6000]}

Верни ТОЛЬКО валидный JSON, без markdown-обёртки, без рассуждений до или после. Первым символом ответа должен быть `{{`, последним `}}`. Формат:
{{
  "status": "answer" | "skip",
  "answer": "..."
}}

Правила:
- для text/textarea: коротко и по делу, без приветствий, до {max_chars} символов;
- для number: только число, без слов и знаков валюты;
- если поле выглядит как свободное (placeholder вроде "Писать тут", "Сообщение") и явного вопроса нет — напиши краткое сопроводительное под вакансию из резюме (3–5 предложений), это не повод для skip;
- если ответ неочевиден и из резюме фактов нет — status=skip;
- НЕ объясняй свой ответ за пределами JSON."""

    try:
        client = get_question_answer_client()
        response = await client.chat.completions.create(
            model=settings.HH_QUESTION_MODEL or settings.LLM_MODEL,
            messages=[
                {"role": "system", "content": "Ты отвечаешь строго в формате JSON. Не пиши никакого текста до или после JSON-объекта."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=600,
        )
        raw_text = response.choices[0].message.content or ""
        model = settings.HH_QUESTION_MODEL or settings.LLM_MODEL
        try:
            parsed = parse_llm_json(raw_text)
        except Exception as parse_exc:
            logger.info("LLM question JSON parse failed, trying repair: %s", parse_exc)
            parsed = await repair_llm_json(
                client,
                model=model,
                raw_text=raw_text,
                parse_error=str(parse_exc),
                schema='{"status": "answer" | "skip", "answer": "..."}',
                max_tokens=600,
            )
    except Exception as exc:
        logger.warning("LLM question answer failed: %s", exc)
        return None

    if parsed.get("status") != "answer":
        return None

    answer = str(parsed.get("answer", "")).strip()
    if not answer:
        return None

    if field_type == "number":
        answer = extract_numeric_salary(answer) if not answer.isdigit() else answer
        if not answer:
            return None
        return answer

    return truncate_text(answer, max_chars)


async def answer_choice_with_llm(
    field: dict,
    resume_text: str,
    page_text: str = "",
    vacancy_context: str = "",
    *,
    settings,
    logger,
    get_question_answer_client,
    build_salary_rule_block,
    build_facts_block,
    build_profile_note_block,
    build_filtered_kb_block,
    build_knowledge_base_block,
    parse_llm_json,
    repair_llm_json,
) -> dict | None:
    """Picks one (or several for checkbox) option(s) for a radio/checkbox/select field.

    Returns dict {"selected": [{"index": int, "custom_text": str | None}], "is_skip": bool}
    or None on error.
    """
    if not settings.HH_AUTO_ANSWER_USE_LLM or not settings.LLM_API_KEY or not resume_text.strip():
        return None

    control = (field.get("control") or "").strip().lower()
    if control not in ("radio", "checkbox", "select"):
        return None

    options = field.get("options") or []
    if not options:
        return None

    question_text = (field.get("question_text") or field.get("placeholder") or "").strip()
    options_block = "\n".join(
        f"  {opt.get('index', i)}: {opt.get('label','')[:200]}"
        + (" [СВОЙ ВАРИАНТ — можно указать свой текст]" if opt.get("is_custom") else "")
        for i, opt in enumerate(options)
    )

    vacancy_block = (
        f"Контекст вакансии (на неё откликаемся):\n{truncate_text(vacancy_context, 1500)}\n\n"
        if vacancy_context else ""
    )
    salary_block = build_salary_rule_block()
    facts_block = build_facts_block()
    profile_note_block = build_profile_note_block()
    # 2-pass: фильтруем KB под конкретную вакансию (если контекст есть)
    try:
        knowledge_block = await build_filtered_kb_block(
            vacancy_context, get_question_answer_client(),
            max_sections=5, limit_chars=8000,
        )
    except Exception as exc:
        logger.debug("filtered KB failed, fallback to full: %s", exc)
        knowledge_block = build_knowledge_base_block(limit_chars=8000)

    multi_hint = (
        "Если уверен в нескольких — верни их в массиве selected."
        if control == "checkbox" else
        "Можно выбрать только ОДИН вариант."
    )

    prompt = f"""Ты выбираешь ответ работодателя на hh.ru от имени кандидата.

Опирайся на структурированные факты, факты из резюме и контекст вакансии. Ничего не выдумывай.
{multi_hint}
Если в вариантах есть «Свой вариант» (помечен [СВОЙ ВАРИАНТ]) — выбирай его и пиши свой текст ТОЛЬКО когда ни один из готовых не подходит, но из фактов/резюме можно ответить.

Тип поля: {control}
Вопрос: {question_text}
Контекст формы: {truncate_text(page_text, 1000) if page_text else "(нет)"}

Варианты ответа:
{options_block}

{profile_note_block}{knowledge_block}{salary_block}{facts_block}{vacancy_block}Резюме кандидата:
{resume_text[:5000]}

Верни ТОЛЬКО валидный JSON, без markdown-обёртки, без рассуждений до или после. Первым символом ответа должен быть `{{`, последним `}}`. Формат:
{{
  "status": "answer" | "skip",
  "selected": [
    {{"index": 0, "custom_text": null}}
  ]
}}

Правила:
- index — номер варианта (целое, как в списке выше);
- custom_text — заполняй ТОЛЬКО если выбран вариант с [СВОЙ ВАРИАНТ], иначе null. Держи custom_text короче 200 символов;
- для radio/select selected содержит ровно один элемент;
- для checkbox — один или несколько элементов;
- если из резюме нельзя выбрать уверенно — status=skip;
- НЕ объясняй свой выбор за пределами JSON."""

    async def _call_llm(user_prompt: str) -> dict | None:
        try:
            client = get_question_answer_client()
            response = await client.chat.completions.create(
                model=settings.HH_CHOICE_MODEL or settings.LLM_MODEL,
                messages=[
                    {"role": "system", "content": "Ты отвечаешь строго в формате JSON. Не пиши никакого текста до или после JSON-объекта."},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=600,
            )
            raw_text = response.choices[0].message.content or ""
            try:
                return parse_llm_json(raw_text)
            except Exception as parse_exc:
                logger.info("LLM choice JSON parse failed, trying repair: %s", parse_exc)
                return await repair_llm_json(
                    client,
                    model=settings.HH_CHOICE_MODEL or settings.LLM_MODEL,
                    raw_text=raw_text,
                    parse_error=str(parse_exc),
                    schema='{"status": "answer" | "skip", "selected": [{"index": 0, "custom_text": null}]}',
                    max_tokens=600,
                )
        except Exception as exc:
            logger.warning("LLM choice answer failed: %s", exc)
            return None

    def _normalize(parsed: dict | None, best_guess: bool) -> dict | None:
        if not parsed:
            return None
        if parsed.get("status") != "answer":
            return {"selected": [], "is_skip": True}
        sel = parsed.get("selected") or []
        if not isinstance(sel, list) or not sel:
            return None
        normalized = []
        max_chars = settings.HH_AUTO_ANSWER_MAX_CHARS
        for item in sel:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= len(options):
                continue
            custom_text = item.get("custom_text")
            if isinstance(custom_text, str):
                custom_text = truncate_text(custom_text.strip(), max_chars)
                if not custom_text:
                    custom_text = None
            else:
                custom_text = None
            normalized.append({"index": idx, "custom_text": custom_text})
        if not normalized:
            return None
        if control != "checkbox":
            normalized = normalized[:1]
        return {"selected": normalized, "is_skip": False, "best_guess": best_guess}

    # First attempt — обычный промпт с разрешённым SKIP.
    result = _normalize(await _call_llm(prompt), best_guess=False)
    if result is None:
        return None
    if not result.get("is_skip"):
        return result

    # Second attempt — best-guess (SKIP запрещён). Только для radio/select; для checkbox
    # отказ от ответа допустим (можно ничего не выбирать).
    if control == "checkbox":
        return result

    logger.info("choice LLM said skip, retrying with best-guess directive")
    retry_prompt = (
        prompt
        + "\n\nВНИМАНИЕ: предыдущая попытка вернула skip. SKIP теперь ЗАПРЕЩЁН. "
        "Возьми лучшее предположение из готовых вариантов (или «Свой вариант» с осторожным "
        "нейтральным текстом). Допустимо ошибиться, лучше прикинуть чем потерять отклик."
    )
    retry = _normalize(await _call_llm(retry_prompt), best_guess=True)
    if retry is None or retry.get("is_skip"):
        # сдаёмся
        return {"selected": [], "is_skip": True}
    return retry


async def try_auto_answer_questions(
    session,
    vacancy_context: str = "",
    *,
    settings,
    load_resume_text,
    anti_bot_message,
) -> dict:
    if not settings.HH_AUTO_ANSWER_SIMPLE_QUESTIONS:
        return {
            "handled": True,
            "ok": False,
            "message": "Требуются доп. вопросы работодателя — пропускаем (автоответ отключён)",
            "notes": [],
        }

    inspected = await session._inspect_employer_questions()
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

    # unsupported_fields теперь почти всегда 0 (C1 переводит radio/checkbox/select в fields).
    # Оставляем early-return только если есть реально неподдерживаемые поля (file/etc.).
    if unsupported_fields:
        unsupported_summary = []
        for item in unsupported_items[:2]:
            question_text = truncate_text(item.get("question_text") or "неизвестный вопрос", 120)
            options = item.get("options") or []
            if options:
                question_text = f"{question_text} [{', '.join(options[:3])}]"
            unsupported_summary.append(question_text)
        return {
            "handled": True,
            "ok": False,
            "message": "Требуются доп. вопросы работодателя — пропускаем (есть неподдерживаемые поля)",
            "notes": [
                "автоответ пропущен: неподдерживаемое поле в форме"
                + (f" ({'; '.join(unsupported_summary)})" if unsupported_summary else "")
            ],
        }

    if total_questions > settings.HH_AUTO_ANSWER_MAX_QUESTIONS:
        return {
            "handled": True,
            "ok": False,
            "message": "Требуются доп. вопросы работодателя — пропускаем (слишком много полей)",
            "notes": [f"автоответ пропущен: полей {total_questions}, лимит {settings.HH_AUTO_ANSWER_MAX_QUESTIONS}"],
        }

    resume_text = load_resume_text()
    salary_text = settings.HH_AUTO_ANSWER_SALARY_TEXT or extract_resume_salary_text(resume_text)
    salary_number = settings.HH_AUTO_ANSWER_SALARY_NUMBER or extract_numeric_salary(salary_text)

    answers = []
    notes = []
    question_answers = []

    for field in fields:
        question_text = (field.get("question_text") or field.get("placeholder") or "").strip()
        input_type = (field.get("input_type") or "text").strip().lower()
        control = (field.get("control") or "").strip().lower()

        if is_risky_question(question_text):
            short_question = truncate_text(question_text or "вопрос по резюме", 100)
            answer_text = "Нужно ручное подтверждение: риск завысить опыт кандидата."
            question_answers.append(
                question_answer_item(
                    question_text or "вопрос по резюме",
                    answer_text,
                    control=control or input_type,
                    required=bool(field.get("required")),
                    starred=bool(field.get("starred")),
                    skipped=True,
                    skip_reason="risky_question",
                )
            )
            return {
                "handled": True,
                "ok": False,
                "message": "Требуются доп. вопросы работодателя — нужно ручное подтверждение рискованного вопроса",
                "notes": [f"автоответ пропущен: рискованный вопрос: {short_question}"],
                "question_answers": question_answers,
                "risky_question": short_question,
            }

        # ---- choice fields (radio / checkbox / select) ----
        if control in ("radio", "checkbox", "select"):
            choice = await session._answer_choice_with_llm(
                field, resume_text, page_text, vacancy_context
            )
            if not choice or choice.get("is_skip") or not choice.get("selected"):
                short_question = truncate_text(question_text or "вопрос по резюме", 100)
                return {
                    "handled": True,
                    "ok": False,
                    "message": "Требуются доп. вопросы работодателя — пропускаем (нет уверенного выбора)",
                    "notes": [f"автоответ пропущен: {short_question}"],
                    "question_answers": question_answers,
                }
            selected = choice["selected"]
            selected_indices = [s["index"] for s in selected]
            # exactly one custom_text per field (first non-null)
            custom_text = next((s.get("custom_text") for s in selected if s.get("custom_text")), None)
            answers.append({
                "field_id": field["field_id"],
                "control": control,
                "selected_indices": selected_indices,
                "custom_text": custom_text,
            })
            options = field.get("options") or []
            picked_labels = [
                (options[i].get("label") if i < len(options) else f"#{i}")
                for i in selected_indices
            ]
            answer_text = ", ".join(picked_labels)
            if custom_text:
                answer_text += f" + custom: {custom_text}"
            bg_mark = " [best-guess]" if choice.get("best_guess") else ""
            notes.append(format_question_answer_note(question_text or "вопрос", answer_text, control=f"{control}{bg_mark}"))
            question_answers.append(
                question_answer_item(
                    question_text or "вопрос",
                    answer_text,
                    control=control,
                    best_guess=bool(choice.get("best_guess")),
                    required=bool(field.get("required")),
                    starred=bool(field.get("starred")),
                )
            )
            continue

        # ---- text / textarea / number ----
        if is_salary_question(question_text):
            answer = salary_number if input_type == "number" else (salary_text or salary_number)
            if not answer:
                return {
                    "handled": True,
                    "ok": False,
                    "message": "Требуются доп. вопросы работодателя — пропускаем (не найден ответ по зарплате)",
                    "notes": ["автоответ пропущен: в резюме нет явного зарплатного ориентира"],
                    "question_answers": question_answers,
                }
            answer_question = "зарплатные ожидания"
            notes.append(format_question_answer_note(answer_question, str(answer)))
        else:
            answer = await session._answer_question_with_llm(field, resume_text, page_text, vacancy_context)
            if not answer:
                short_question = truncate_text(question_text or "вопрос по резюме", 100)
                return {
                    "handled": True,
                    "ok": False,
                    "message": "Требуются доп. вопросы работодателя — пропускаем (нет уверенного ответа)",
                    "notes": [f"автоответ пропущен: {short_question}"],
                    "question_answers": question_answers,
                }
            answer_question = question_text or "вопрос по резюме"
            notes.append(format_question_answer_note(answer_question, str(answer)))

        answers.append({"field_id": field["field_id"], "answer": answer})
        question_answers.append(
            question_answer_item(
                answer_question,
                str(answer),
                control=control or input_type,
                required=bool(field.get("required")),
                starred=bool(field.get("starred")),
            )
        )

    fill_result = await session._fill_employer_question_answers(answers)
    if int(fill_result.get("filled", 0)) != len(answers):
        return {
            "handled": True,
            "ok": False,
            "message": "Требуются доп. вопросы работодателя — пропускаем (не удалось заполнить форму)",
            "notes": notes + [f"ошибка заполнения: {', '.join(fill_result.get('errors', [])[:2])}"],
            "question_answers": question_answers,
        }

    await session._page.wait_for_timeout(500)

    if not await session._submit_employer_questions():
        return {
            "handled": True,
            "ok": False,
            "message": "Требуются доп. вопросы работодателя — пропускаем (не удалось отправить форму)",
            "notes": notes,
            "question_answers": question_answers,
        }

    await session._page.wait_for_timeout(4000)

    anti_bot_kind = await session._detect_anti_bot_kind()
    anti_bot_kind = await session._handle_anti_bot_with_solver(anti_bot_kind, stage="questions_submit")
    if anti_bot_kind:
        message = anti_bot_message(anti_bot_kind, "после автоответа на вопросы")
        session._remember_antibot_signal(anti_bot_kind, "questions_submit", message)
        return {
            "handled": True,
            "ok": False,
            "message": message,
            "notes": notes,
            "question_answers": question_answers,
            "anti_bot_kind": anti_bot_kind,
        }

    if await session._apply_success_detected() or await session._has_existing_response_ui():
        return {
            "handled": True,
            "ok": True,
            "message": "Отклик отправлен",
            "notes": notes,
            "question_answers": question_answers,
        }

    if await session._response_requires_questions():
        return {
            "handled": True,
            "ok": False,
            "message": "Требуются доп. вопросы работодателя — пропускаем (форма не закрылась после автоответа)",
            "notes": notes,
            "question_answers": question_answers,
        }

    return {
        "handled": True,
        "ok": False,
        "message": "Не удалось подтвердить отклик после автоответа на вопросы",
        "notes": notes,
        "question_answers": question_answers,
    }
