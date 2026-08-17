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
