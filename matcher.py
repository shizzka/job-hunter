"""LLM-оценка релевантности вакансии + генерация сопроводительного письма."""
import hashlib
import json
import logging
import os
import re

import config
from llm_client import LLMProvidersExhaustedError, get_llm_client

log = logging.getLogger("matcher")

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = get_llm_client()
    return _client


def _load_resume() -> str:
    """Загрузить резюме из файла."""
    if os.path.exists(config.RESUME_FILE):
        with open(config.RESUME_FILE) as f:
            return f.read().strip()
    return f"(Резюме не найдено — заполни {config.RESUME_FILE})"


from llm_utils import parse_llm_json as _parse_llm_json  # re-export для обратной совместимости


ONE_YEAR_EXPERIENCE_PATTERNS = [
    r"\b1\s*\+?\s*year\b",
    r"\b1\s*\+?\s*years\b",
    r"\bone year\b",
    r"\bjunior\b",
    r"\bопыт\s+от\s+1\s+года\b",
    r"\bопыт\s+работы\s+от\s+1\s+года\b",
    r"\bот\s+1\s+года\b",
    r"\b1\s*[-–]\s*3\s*года\b",
    r"\b1\s*[-–]\s*3\s*years\b",
    r"\bдо\s+1\s+года\b",
]

SENIOR_EXPERIENCE_PATTERNS = [
    r"\bsenior\b",
    r"\blead\b",
    r"\bprincipal\b",
    r"\bstaff\b",
    r"\bстарш(ий|ая)\b",
    r"\bведущ(ий|ая)\b",
    r"\bsenior\s+qa\b",
    r"\bqa\s+senior\b",
]

MIDDLE_EXPERIENCE_PATTERNS = [
    r"\bmiddle\b",
    r"\bmid-level\b",
    r"\bmid level\b",
    r"\bмидл\b",
    r"\bмиддл\b",
    r"\bmiddle\s+qa\b",
    r"\bqa\s+middle\b",
    r"\bопыт\s+от\s+2\s+лет\b",
    r"\bот\s+2\s+лет\b",
    r"\b2\s*[-–]\s*3\s*года\b",
    r"\b3\s*[-–]\s*6\s*лет\b",
]


AUTOMATION_HEAVY_PATTERNS = [
    r"\bqa\s+auto\b",
    r"\baqa\b",
    r"\bsdet\b",
    r"\bautomation\s+(qa|engineer|tester)\b",
    r"\bqa\s+automation\b",
    r"\bавтоматизатор\b",
    r"\b(?:писать|разрабатывать|создавать|поддерживать|проектировать)\w*\b.{0,120}\b(?:автотест|selenium|playwright|cypress|pytest)\b",
    r"\b(?:автотест|selenium|playwright|cypress|pytest)\b.{0,120}\b(?:писать|разрабатывать|создавать|поддерживать|проектировать)\w*\b",
    r"\b(?:selenium|playwright|cypress)\b",
    r"\b(?:java|typescript|ts)\b.{0,80}\b(?:автотест|selenium|playwright|automation)\b",
    r"\b(?:автотест|selenium|playwright|automation)\b.{0,80}\b(?:java|typescript|ts)\b",
]

AUTOMATION_ROLE_TITLE_PATTERNS = [
    r"\bqa\s+auto\b",
    r"\baqa\b",
    r"\bsdet\b",
    r"\bautomation\s+(qa|engineer|tester)\b",
    r"\bqa\s+automation\b",
    r"\bавтоматизатор\b",
    r"\bавтотестировщик\b",
    r"\bавтоматизированн\w*\s+тестировани\w*\b",
    r"\bавтоматизац\w*\s+тестировани\w*\b",
]

AUTOMATION_OPTIONAL_HINT_PATTERNS = [
    r"будет\s+плюсом",
    r"будет\s+преимуществом",
    r"как\s+плюс",
    r"желательно",
    r"optional",
    r"nice\s+to\s+have",
    r"не\s+обязательно",
]

AUTOMATION_HARD_ACTION_PATTERNS = [
    r"\b(?:писать|разрабатывать|создавать|поддерживать|проектировать)\w*\b.{0,120}\b(?:автотест|selenium|playwright|cypress|pytest)\b",
    r"\b(?:автотест|selenium|playwright|cypress|pytest)\b.{0,120}\b(?:писать|разрабатывать|создавать|поддерживать|проектировать)\w*\b",
    r"\b(?:java|typescript|ts)\b.{0,80}\b(?:автотест|selenium|playwright|automation)\b",
    r"\b(?:автотест|selenium|playwright|automation)\b.{0,80}\b(?:java|typescript|ts)\b",
]

JUNIOR_OR_TRAINING_PATTERNS = [
    r"\bjunior\b",
    r"\btrainee\b",
    r"\bстаж[её]р\b",
    r"\bмладш(ий|ая)\b",
    r"\bджун\b",
    r"\bбез\s+опыта\b",
    r"\bготовы\s+обуч(ать|ить)\b",
    r"\bобуч(аем|ение|ать)\b",
    r"\bдо\s+1\s+года\b",
    r"\bопыт\s+от\s+1\s+года\b",
]

CANDIDATE_CLAIM_OVERSTATEMENT_PATTERNS = [
    (
        "завышенный QA-стаж",
        [
            r"\b(?:более|больше|свыше|от)\s+(?:1|2|3|4|5|одного|двух|тр[её]х)\s+(?:года|лет|years?)\b",
            r"\b(?:более|больше|свыше)\s+года\b",
            r"\b(?:2|3|4|5)\s*\+?\s*(?:года|лет|years?)\b",
            r"\b(?:двух|тр[её]х|четыр[её]х|пяти)\s+лет\b",
            r"\bмноголетн\w*\s+опыт\b",
        ],
    ),
    (
        "завышенный опыт автоматизации",
        [
            r"\b(?:qa\s*automation|automation\s*qa|sdet|aqa)\b",
            r"\b(?:уверенно|самостоятельно|полноценн\w*|production|продакшн)\b.{0,120}\b(?:автотест|автоматизац|automation|pytest|python)\b",
            r"\b(?:пишу|пишет|писал\w*|разрабатывал\w*|разрабатывает|создавал\w*|созда[её]т|поддерживал\w*)\b.{0,120}\b(?:автотест|автоматизац|automation|pytest)\b",
            r"\b(?:имеет|есть|обладает|имею|работал\w*)\b.{0,120}\b(?:selenium|playwright|cypress|java\s+automation|java\s+автотест)\b",
            r"\b(?:selenium|playwright|cypress|java\s+automation|java\s+автотест)\b.{0,120}\b(?:имеет|есть|обладает|опыт|работал\w*)\b",
        ],
    ),
    (
        "неподтвержденный IT/QA leadership",
        [
            r"\b(?:qa|it|айти)\b.{0,80}\b(?:lead|лид|тимлид|руководил\w*|управлял\w*)\b",
            r"\b(?:lead|лид|тимлид|руководил\w*|управлял\w*)\b.{0,80}\b(?:qa|it|айти|команд[а-я]*)\b",
            r"\b(?:scrum\s*master|скрам\s*мастер|project\s*manager|product\s*owner)\b",
        ],
    ),
    (
        "неподтвержденное высшее техническое образование",
        [
            r"\bвысш\w*\s+техническ\w*\s+образовани\w*\b",
        ],
    ),
]


def _is_one_year_experience_vacancy(vacancy: dict, details: str = "") -> bool:
    haystack = " ".join(
        part
        for part in (
            vacancy.get("title", ""),
            vacancy.get("snippet", ""),
            details or "",
        )
        if part
    ).lower()
    return any(re.search(pattern, haystack) for pattern in ONE_YEAR_EXPERIENCE_PATTERNS)


def _is_senior_experience_vacancy(vacancy: dict, details: str = "") -> bool:
    haystack = " ".join(
        part
        for part in (
            vacancy.get("title", ""),
            vacancy.get("snippet", ""),
            details or "",
        )
        if part
    ).lower()
    return any(re.search(pattern, haystack) for pattern in SENIOR_EXPERIENCE_PATTERNS)


def _is_middle_experience_vacancy(vacancy: dict, details: str = "") -> bool:
    haystack = " ".join(
        part
        for part in (
            vacancy.get("title", ""),
            vacancy.get("snippet", ""),
            details or "",
        )
        if part
    ).lower()
    return any(re.search(pattern, haystack) for pattern in MIDDLE_EXPERIENCE_PATTERNS)


def _vacancy_haystack(vacancy: dict, details: str = "") -> str:
    return " ".join(
        part
        for part in (
            vacancy.get("title", ""),
            vacancy.get("company", ""),
            vacancy.get("snippet", ""),
            details or "",
        )
        if part
    ).casefold()


def _is_automation_heavy_vacancy(vacancy: dict, details: str = "") -> bool:
    title = str(vacancy.get("title", "")).casefold()
    haystack = _vacancy_haystack(vacancy, details)
    if any(re.search(pattern, title) for pattern in AUTOMATION_ROLE_TITLE_PATTERNS):
        return True

    has_hard_action = any(re.search(pattern, haystack) for pattern in AUTOMATION_HARD_ACTION_PATTERNS)
    if has_hard_action:
        return True

    has_optional_hint = any(re.search(pattern, haystack) for pattern in AUTOMATION_OPTIONAL_HINT_PATTERNS)
    if has_optional_hint:
        return False

    return any(re.search(pattern, haystack) for pattern in AUTOMATION_HEAVY_PATTERNS)


def _is_junior_or_training_vacancy(vacancy: dict, details: str = "") -> bool:
    haystack = _vacancy_haystack(vacancy, details)
    return any(re.search(pattern, haystack) for pattern in JUNIOR_OR_TRAINING_PATTERNS)


def _coerce_score(value, default: int = 50) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = default
    return max(0, min(100, score))


SALARY_SOFT_FLAG_PATTERNS = [
    r"зарп",
    r"оплат",
    r"компенсац",
    r"доход",
    r"вилк",
]

HARD_MONEY_RED_FLAG_PATTERNS = [
    r"мошен",
    r"треб(уют|уется).*деньг",
    r"взнос",
    r"предоплат",
    r"платн(ое|ая|ый).*обуч",
]


def _add_guard_flag(result: dict, flag: str) -> None:
    guard_flags = result.setdefault("guard_flags", [])
    if flag not in guard_flags:
        guard_flags.append(flag)


def _append_reason(result: dict, note: str) -> None:
    reason = str(result.get("reason", "")).strip()
    if note in reason:
        return
    result["reason"] = f"{reason} {note}".strip() if reason else note


def _is_salary_soft_flag(flag) -> bool:
    text = str(flag or "").casefold()
    if not text:
        return False
    has_salary_hint = any(re.search(pattern, text) for pattern in SALARY_SOFT_FLAG_PATTERNS)
    has_hard_money_risk = any(re.search(pattern, text) for pattern in HARD_MONEY_RED_FLAG_PATTERNS)
    return has_salary_hint and not has_hard_money_risk


def _soften_salary_red_flags(result: dict) -> dict:
    red_flags = [str(flag).strip() for flag in result.get("red_flags", []) if str(flag).strip()]
    if not red_flags:
        result["red_flags"] = []
        return result

    softened = [flag for flag in red_flags if _is_salary_soft_flag(flag)]
    if not softened:
        result["red_flags"] = red_flags
        return result

    result["red_flags"] = [flag for flag in red_flags if flag not in softened]
    soft_flags = result.setdefault("soft_flags", [])
    for flag in softened:
        if flag not in soft_flags:
            soft_flags.append(flag)
    result["score"] = max(0, _coerce_score(result.get("score"), default=50) - 5)
    _add_guard_flag(result, "salary_red_flag_softened")
    _append_reason(result, "Зарплатную вилку учитываем как мягкий минус, не как стоп-фактор.")
    return result


def _apply_auto_apply_threshold(result: dict) -> dict:
    threshold = max(50, _coerce_score(getattr(config, "HH_MATCHER_AUTO_APPLY_MIN_SCORE", 58), default=58))
    if result.get("should_apply") and _coerce_score(result.get("score"), default=0) < threshold:
        result["should_apply"] = False
        _add_guard_flag(result, "below_auto_apply_threshold")
        _append_reason(result, f"Score ниже порога автоотклика {threshold}.")
    return result


def _middle_challenge_threshold() -> int:
    auto_threshold = _coerce_score(getattr(config, "HH_MATCHER_AUTO_APPLY_MIN_SCORE", 58), default=58)
    middle_threshold = _coerce_score(getattr(config, "HH_MATCHER_MIDDLE_CHALLENGE_MIN_SCORE", 60), default=60)
    return max(auto_threshold, middle_threshold)


def _build_matcher_truth_block() -> str:
    parts = [
        "## Контрольные факты кандидата для оценки:",
        "- Профиль: Junior Manual QA / Manual QA.",
        "- QA-стаж: около 1 года практического тестирования. Нельзя писать 2+, 3+, многолетний или senior QA-стаж.",
        "- Автоматизация: можно писать, что кандидат участвовал в разработке API-автотестов на Python/pytest, помогал со сценариями/покрытием, настраивал окружение и запускал готовые тесты. Нельзя позиционировать как самостоятельного QA Automation/SDET или заявлять Selenium/Java/Playwright/Cypress.",
        "- Инженерный и электротехнический опыт можно учитывать как технический бэкграунд и диагностику, но не как QA Lead / IT Team Lead / Scrum Master.",
        "- Образование техническое среднее специальное; не писать про высшее техническое образование.",
        "- Если ключевое требование вакансии - самостоятельная разработка/поддержка production-автотестов или роль SDET/QA Automation, а вакансия не junior/trainee/с обучением, ставь should_apply=false.",
    ]
    try:
        from prompt_blocks import build_facts_block, build_profile_note_block

        profile_note = build_profile_note_block().strip()
        facts = build_facts_block().strip()
        if profile_note:
            parts.append(profile_note)
        if facts:
            parts.append(facts)
    except Exception as exc:
        log.debug("matcher profile facts load failed: %s", exc)
    return "\n".join(parts) + "\n\n"


def _detect_candidate_claim_overstatements(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", (text or "").casefold()).strip()
    if not normalized:
        return []
    findings = []
    for label, patterns in CANDIDATE_CLAIM_OVERSTATEMENT_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, normalized):
                findings.append(label)
                break
    return sorted(set(findings))


def _block_result(result: dict, note: str, guard_flag: str) -> dict:
    result["score"] = min(_coerce_score(result.get("score"), default=0), 39)
    result["should_apply"] = False
    _add_guard_flag(result, guard_flag)
    _append_reason(result, note)
    return result


def _apply_candidate_truth_guards(result: dict, vacancy: dict, details: str = "") -> dict:
    if _is_automation_heavy_vacancy(vacancy, details) and not _is_junior_or_training_vacancy(vacancy, details):
        return _block_result(
            result,
            "Guard: ключевое требование похоже на самостоятельную production-автоматизацию/AQA, это выше подтвержденного уровня кандидата.",
            "automation_heavy_mismatch",
        )

    if result.get("should_apply"):
        overstatements = _detect_candidate_claim_overstatements(str(result.get("reason", "")))
        if overstatements:
            joined = ", ".join(overstatements)
            return _block_result(
                result,
                f"Guard: LLM завысила профиль кандидата ({joined}).",
                "candidate_claim_overstatement",
            )
    return result


def _fallback_cover_letter(vacancy: dict, details: str = "") -> str:
    return (
        "По вакансии вижу задачи по тестированию продукта и аккуратной проверке сценариев. "
        "Мой профиль - Junior Manual QA: около 1 года практики, ручные проверки, тест-кейсы, "
        "баг-репорты и технический бэкграунд в диагностике. "
        "Если такой уровень подходит, обсудим задачи и формат работы."
    )


COVER_LETTER_STYLE_VARIANTS = (
    {
        "name": "product_hook",
        "opening": "Начни с конкретного продукта/домена из вакансии: что там тестировать или сопровождать.",
        "shape": "1) домен вакансии -> 2) похожий кусок текущего опыта -> 3) один способ проверки -> 4) короткий следующий шаг.",
        "ending": "Финал без 'готов': 'можно обсудить детали', 'расскажу подробнее на созвоне', 'напишите, если такой профиль подходит'.",
    },
    {
        "name": "qa_risk",
        "opening": "Начни с QA-риска или пользовательского сценария из вакансии, не с рассказа о себе.",
        "shape": "1) где может ломаться качество -> 2) как кандидат это проверяет руками/API -> 3) один релевантный инструмент -> 4) спокойный next step.",
        "ending": "Финал короткий, без обещаний 'принести пользу'.",
    },
    {
        "name": "current_work_mirror",
        "opening": "Начни с 'Сейчас у меня...' или близкой живой фразы про похожую задачу, но не используй 'в текущем проекте'.",
        "shape": "1) похожая задача сейчас -> 2) чем она пересекается с вакансией -> 3) один факт из инженерного/QA опыта -> 4) короткий next step.",
        "ending": "Финал в стиле обычного сообщения HR-у, без торжественности.",
    },
    {
        "name": "tooling_detail",
        "opening": "Начни с конкретной проверки или рабочего процесса: API, запросы, баги, тест-кейсы, регресс.",
        "shape": "1) практическая проверка -> 2) где это нужно в вакансии -> 3) 1-2 инструмента максимум -> 4) короткая фраза про обсуждение.",
        "ending": "Не заканчивай словом 'готов'; лучше живой короткий финал.",
    },
    {
        "name": "engineering_background",
        "opening": "Если уместно, начни с инженерного прошлого и диагностики, а потом переведи в QA.",
        "shape": "1) диагностика/поиск причины -> 2) как это помогает в тестировании -> 3) связь с вакансией -> 4) короткий next step.",
        "ending": "Финал без клише про вклад в команду.",
    },
    {
        "name": "direct_fit",
        "opening": "Начни прямо: 'По вакансии вижу...' или аналогично, с одного конкретного совпадения.",
        "shape": "1) одно совпадение с вакансией -> 2) один факт из опыта -> 3) одно ограничение/честная рамка если нужно -> 4) короткий next step.",
        "ending": "Финал должен звучать как короткое сообщение, не как мотивационное письмо.",
    },
)


def _cover_letter_variant_index(vacancy: dict, details: str = "") -> int:
    seed = "|".join(
        str(part or "")
        for part in (
            vacancy.get("id"),
            vacancy.get("url"),
            vacancy.get("title"),
            vacancy.get("company"),
            vacancy.get("snippet"),
            (details or "")[:500],
        )
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % len(COVER_LETTER_STYLE_VARIANTS)


def _build_cover_letter_style_block(vacancy: dict, details: str = "") -> str:
    variant = COVER_LETTER_STYLE_VARIANTS[_cover_letter_variant_index(vacancy, details)]
    return f"""## Вариант стиля для ЭТОГО письма:
- Стратегия: {variant['name']}
- Как начать: {variant['opening']}
- Скелет: {variant['shape']}
- Как закончить: {variant['ending']}
- Не копируй пример ниже.
- НЕ начинай письмо с дежурных заходов: "Заметил", "Увидел", "Вижу", "Привет", "В вашей вакансии", "В вашей команде", "С большим интересом", "Я QA".
- Первое предложение должно сразу называться по сути выбранной стратегии: домен, риск, процесс, текущая похожая задача или инженерная диагностика.
- Варьируй ритм: одно предложение может быть совсем коротким; не делай все письма одинаковой длины и структуры.
"""


async def evaluate_vacancy(vacancy: dict, details: str = "") -> dict:
    """
    Оценить вакансию на релевантность.

    Возвращает:
    {
        "score": 0-100,       # оценка релевантности
        "reason": "...",      # почему подходит/не подходит
        "should_apply": bool, # рекомендация
        "red_flags": [...]    # красные флаги если есть
    }
    """
    resume = _load_resume()
    truth_block = _build_matcher_truth_block()

    allow_one_year_override = _is_one_year_experience_vacancy(vacancy, details)
    force_senior_reject = _is_senior_experience_vacancy(vacancy, details)
    force_middle_reject = _is_middle_experience_vacancy(vacancy, details) and not _is_junior_or_training_vacancy(vacancy, details)

    prompt = f"""Ты — ассистент по поиску работы. Оцени подходит ли вакансия для кандидата.

{truth_block}## Резюме кандидата:
{resume}

## Вакансия:
Название: {vacancy.get('title', '—')}
Компания: {vacancy.get('company', '—')}
Зарплата: {vacancy.get('salary', 'не указана')}
Краткое описание: {vacancy.get('snippet', '—')}

## Полное описание вакансии:
{details[:2000] if details else '(нет деталей)'}

## Задача:
Оцени вакансию по шкале 0-100, где:
- 80-100: отличное совпадение, точно откликаться
- 60-79: хорошее совпадение, стоит откликнуться
- 40-59: среднее совпадение, можно попробовать
- 0-39: не подходит

Верни ТОЛЬКО валидный JSON, без markdown-обёртки, без префикса, без рассуждений до или после. Первым символом ответа должен быть `{{`, последним `}}`. Формат:
{{
    "score": <число 0-100>,
    "reason": "<1-2 предложения почему подходит/не подходит>",
    "should_apply": <true/false>,
    "red_flags": ["<красный флаг 1>", ...]
}}

Красные флаги: мошенники, MLM, требуют деньги от кандидата, вакансия-ловушка, явно критически неподходящая оплата.
Отсутствие зарплатной вилки само по себе НЕ red_flag: учитывай как мягкий минус в score/reason.
ЖЁСТКИЙ красный флаг (score=0, should_apply=false): всё связанное с войной, СВО, боевыми действиями, ВПК, оборонкой, военной службой, военными организациями."""

    if allow_one_year_override:
        prompt += """

Дополнительное правило:
- Требование опыта до 1 года, junior/trainee-уровень или обучение НЕ считать причиной для отказа само по себе.
- Диапазон 1-3 года не режь только по цифре, но не завышай профиль кандидата.
- Middle/mid-level или 2+ года именно QA без junior/trainee считай challenge-вакансией: высокий score ставь только если это manual/API/technical QA без самостоятельной AQA/SDET и совпадение действительно сильное.
- Если вакансия в целом QA/тестовая и выглядит адекватной, оценивай её по реальному совпадению, не по шаблонному отказу."""

    if force_senior_reject:
        prompt += """

Дополнительное правило:
- Senior / Lead / Principal-уровень считать несоответствием профилю кандидата.
- Такие вакансии не рекомендовать к отклику, если только текст явно не противоречит senior-метке."""

    if force_middle_reject:
        prompt += """

Дополнительное правило для Middle / 2+ years:
- Это challenge-вакансия, а не автоматический отказ.
- Если вакансия про Manual QA, API QA, веб/API, требования, тестовую документацию, SQL, Postman/DevTools и НЕ требует самостоятельной роли AQA/SDET/QA Automation, можно ставить высокий score при сильном совпадении.
- Инженерный и электротехнический бэкграунд кандидата, диагностику, работу с требованиями и руководство командой в электрике учитывай как плюс к системности, но НЕ как 2+ года QA.
- В reason честно пиши: около 1 года QA + сильный технический/диагностический бэкграунд; не придумывай Middle QA-стаж."""

    try:
        client = _get_client()
        resp = await client.chat.completions.create(
            model=config.HH_MATCHER_MODEL or config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=800,
        )
        text = resp.choices[0].message.content.strip()
        result = _parse_llm_json(text)
        if not isinstance(result, dict):
            raise ValueError("LLM response JSON is not an object")
        result["score"] = _coerce_score(result.get("score"), default=50)
        result.setdefault("reason", "")
        result["should_apply"] = bool(result.get("should_apply", result["score"] >= 50))
        if result["score"] < 50:
            result["should_apply"] = False
        red_flags = result.get("red_flags", [])
        if not isinstance(red_flags, list):
            red_flags = [str(red_flags)] if red_flags else []
        result["red_flags"] = [str(flag).strip() for flag in red_flags if str(flag).strip()]
        result = _soften_salary_red_flags(result)
        if (
            allow_one_year_override
            and not result["red_flags"]
            and result["score"] >= 40
        ):
            result["score"] = max(result["score"], 50)
            result["should_apply"] = True
            reason = result.get("reason", "").strip()
            note = "Не режем вакансию только из-за требования опыта до 1 года."
            result["reason"] = f"{reason} {note}".strip()
        if force_senior_reject:
            result["score"] = min(result.get("score", 0), 39)
            result["should_apply"] = False
            _add_guard_flag(result, "senior_level_mismatch")
            _append_reason(result, "Senior/Lead-уровень считаем слишком высоким для текущего профиля.")
        if force_middle_reject:
            middle_threshold = _middle_challenge_threshold()
            if result["score"] >= middle_threshold and not result.get("red_flags"):
                _add_guard_flag(result, "middle_challenge_allowed")
                _append_reason(result, "Middle/2+ берём как challenge-отклик: честно без завышения QA-стажа.")
            else:
                result["score"] = min(result.get("score", 0), middle_threshold - 1)
                result["should_apply"] = False
                _add_guard_flag(result, "middle_challenge_below_threshold")
                _append_reason(result, f"Middle/2+ challenge требует score >= {middle_threshold} и отсутствия red flags.")
        result = _apply_candidate_truth_guards(result, vacancy, details)
        return _apply_auto_apply_threshold(result)

    except LLMProvidersExhaustedError as e:
        log.error("LLM providers exhausted for evaluation: %s", e)
        providers = ", ".join(e.provider_names)
        return {
            "score": 0,
            "reason": (
                f"LLM лимиты исчерпаны для модели {e.model or 'unknown'}"
                f"; провайдеры: {providers or 'нет'}"
            ),
            "should_apply": False,
            "red_flags": [],
            "error_kind": "llm_limits_exhausted",
            "llm_model": e.model,
            "llm_providers": list(e.provider_names),
            "llm_error": e.last_error,
        }

    except Exception as e:
        log.error("LLM evaluation failed: %s", e)
        return {
            "score": 0,
            "reason": f"LLM ошибка: {e}",
            "should_apply": False,  # при ошибке LLM — не откликаться вслепую
            "red_flags": [],
            "error_kind": "llm_error",
            "llm_model": config.HH_MATCHER_MODEL or config.LLM_MODEL,
            "llm_error": str(e),
        }


async def generate_cover_letter(vacancy: dict, details: str = "") -> str:
    """Сгенерировать сопроводительное письмо для вакансии."""
    resume = _load_resume()
    from prompt_blocks import (
        build_profile_note_block,
        build_filtered_kb_block,
        build_knowledge_base_block,
    )
    profile_note = build_profile_note_block()
    # 2-pass: фильтруем KB-секции под конкретную вакансию через LLM
    vacancy_summary = (
        f"Должность: {vacancy.get('title', '')}\n"
        f"Компания: {vacancy.get('company', '')}\n"
        f"Описание: {(details or vacancy.get('snippet', ''))[:1200]}"
    )
    try:
        client = _get_client()
        knowledge = await build_filtered_kb_block(
            vacancy_summary, client, max_sections=5, limit_chars=8000,
        )
    except Exception as exc:
        log.warning("filtered KB selection failed, fallback to full: %s", exc)
        knowledge = build_knowledge_base_block(limit_chars=8000)

    style_block = _build_cover_letter_style_block(vacancy, details)

    prompt = f"""Ты — ассистент по поиску работы. Напиши короткое сопроводительное письмо.

{profile_note}{knowledge}{style_block}## Резюме кандидата:
{resume}

## Вакансия:
Название: {vacancy.get('title', '—')}
Компания: {vacancy.get('company', '—')}

## Описание вакансии:
{details[:1500] if details else vacancy.get('snippet', '(нет описания)')}

## Длина и форма:
- 3-4 предложения, до 1500 символов
- Писать от первого лица, в разговорном профессиональном тоне (как сообщение HR-у в мессенджере, не сочинение)
- Использовать обычное тире "-", НЕ em-dash "—" и НЕ дефис между словами как разделитель
- Пиши обычные слова: "REST API", "тест-кейсы", не "REST‑API", не "тест‑кейсы" со спец-символом

## ЖЁСТКИЕ ОГРАНИЧЕНИЯ ПРОТИВ ВЫДУМЫВАНИЯ:
- Используй ТОЛЬКО факты из раздела "Резюме кандидата" выше.
- ЗАПРЕЩЕНО упоминать технологии, языки, фреймворки, инструменты, которых нет в резюме, даже если они в вакансии. Если в вакансии Java/Selenium/Kotlin, а в резюме их нет - НЕ ПИСАТЬ про них.
- ЗАПРЕЩЕНО завышать стаж. Бери срок строго из резюме.
- ЗАПРЕЩЕНО приписывать достижения, цифры, проекты, которых нет в резюме.

## АНТИ-AI ПРАВИЛА (НЕ писать как LLM):
НЕЛЬЗЯ начинать письмо с самопредставления "Я - QA Engineer", "Я QA с опытом", "Меня зовут". Начни с дела: что зацепило в вакансии, или с конкретного факта про себя, релевантного позиции.
НЕЛЬЗЯ использовать слова-паразиты: "активно", "регулярно", "успешно", "эффективно", "оперативно", "профессионально", "качественно", "ежедневно", "глубокий опыт", "обширный опыт".
НЕЛЬЗЯ использовать шаблонные обороты: "в текущем проекте я", "в текущем проекте", "на текущем проекте", "благодаря этому", "это позволяет мне", "имею опыт", "обладаю навыками", "в моём арсенале", "владею инструментами", "готов применить навыки", "внести вклад в команду", "с большим интересом", "буду рад", "имею удовольствие". Если хочешь сослаться на текущую работу - пиши "сейчас работаю над...", "у меня сейчас...", "щас на проекте...".
НЕЛЬЗЯ строить письмо по тройной схеме "опыт → перечень технологий → готовность".
НЕЛЬЗЯ перечислять подряд через запятую больше 3 инструментов - живой человек так не пишет.
НЕЛЬЗЯ заканчивать на "готов изучить", "готов работать", "буду полезен".

## Как пишет живой кандидат:
- Короткие предложения. Иногда совсем короткие.
- Конкретика вместо обобщений: не "тестирую web-приложения", а "сейчас гоняю чаты и тарифную систему".
- Цифры и конкретные имена инструментов из резюме - но 1-2 за всё письмо, не списком.
- Допустимы лёгкие неформальные обороты: "поковырял", "сижу на", "проверяю руками".
- Можно начать с реакции на вакансию: "Увидел у вас X - у меня было похожее на Y."

Пример хорошего стиля (НЕ копировать дословно, это шаблон стиля):
"Заметил вакансию - у вас как раз веб с авторизацией и личным кабинетом. У меня сейчас примерно такой же стек: чаты, тарифы, REST. Тест-кейсы веду в TestIT, баги в YouTrack, ручное + смотрю запросы в DevTools и Чарльзе. До QA 13 лет занимался диагностикой техники, привычка докапываться до причины осталась. Если интересно - готов созвониться."

Пример плохого стиля (ЗАПРЕЩЕНО так писать):
"Я - QA Engineer с более чем 13-летним опытом в технической диагностике и QA. В текущем проекте я активно поддерживаю более 360 тест-кейсов в TestIT, ежедневно тестирую функциональность web-приложения и проверяю REST-API через Postman, а также анализирую сетевые запросы. Благодаря этому я готов применить свои навыки в вашей команде."

Напиши ТОЛЬКО текст письма, без заголовков и пояснений."""

    try:
        client = _get_client()
        resp = await client.chat.completions.create(
            model=config.HH_COVER_LETTER_MODEL or config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.55,
            max_tokens=2000,
        )
        cover = resp.choices[0].message.content.strip()
        overstatements = _detect_candidate_claim_overstatements(cover)
        if overstatements:
            log.warning("cover letter overclaim guard triggered: %s", overstatements)
            return _fallback_cover_letter(vacancy, details)
        return cover

    except Exception as e:
        log.error("Cover letter generation failed: %s", e)
        return ""
