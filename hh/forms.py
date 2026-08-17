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
