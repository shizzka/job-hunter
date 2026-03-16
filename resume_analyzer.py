"""
Анализ резюме с помощью LLM.

Промт загружается из внешнего файла ~/.job-hunter/resume_prompt.md
(не коммитится в репозиторий).
"""
import logging
import os

from openai import AsyncOpenAI

import config

log = logging.getLogger("resume_analyzer")

# Путь к файлу с промтом (вне репозитория)
PROMPT_FILE = os.path.join(config.JOB_HUNTER_HOME, "resume_prompt.md")

# Минимальный fallback, если файл не найден
_FALLBACK_SYSTEM = "Ты — карьерный консультант и ATS-аналитик. Проанализируй резюме, укажи слабые места, предложи улучшения."
_FALLBACK_USER = """Резюме:
{resume}

Вакансия:
{vacancy}

Страна / рынок:
{market}

Уровень позиции:
{level}"""


def _load_prompt() -> tuple[str, str]:
    """
    Загрузить промт из файла.

    Формат файла: system-промт и user-промт разделены строкой '---'.
    Если разделителя нет — весь файл считается system-промтом,
    user-промт берётся fallback.

    Возвращает (system_prompt, user_prompt_template).
    """
    if not os.path.isfile(PROMPT_FILE):
        log.warning(
            "Файл промта не найден: %s — используется базовый промт. "
            "Положи свой промт в этот файл для полного анализа.",
            PROMPT_FILE,
        )
        return _FALLBACK_SYSTEM, _FALLBACK_USER

    with open(PROMPT_FILE, encoding="utf-8") as f:
        content = f.read()

    if "\n---\n" in content:
        system_part, user_part = content.split("\n---\n", 1)
        return system_part.strip(), user_part.strip()

    # Весь файл — system prompt
    return content.strip(), _FALLBACK_USER


def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY or "no-key",
    )


async def analyze_resume(
    resume_text: str,
    vacancy_text: str = "",
    market: str = "Россия / СНГ",
    level: str = "",
) -> str:
    """
    Полный анализ резюме через LLM.

    Возвращает текст анализа с рекомендациями и улучшенной версией.
    """
    if not resume_text.strip():
        return "Резюме пустое — нечего анализировать."

    if not level:
        level = "определи по содержанию резюме"

    system_prompt, user_template = _load_prompt()

    user_prompt = user_template.format(
        resume=resume_text,
        vacancy=vacancy_text or "(не указана — адаптируй под наиболее очевидную целевую роль из резюме)",
        market=market,
        level=level,
    )

    client = _get_client()

    try:
        response = await client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=8000,
        )
        return response.choices[0].message.content or "(пустой ответ LLM)"
    except Exception as e:
        log.error("Resume analysis failed: %s", e)
        return f"Ошибка анализа: {e}"


async def analyze_resume_file(
    resume_path: str,
    vacancy_text: str = "",
    market: str = "Россия / СНГ",
    level: str = "",
) -> str:
    """Загрузить резюме из файла и проанализировать."""
    if not os.path.isfile(resume_path):
        return f"Файл не найден: {resume_path}"

    with open(resume_path, encoding="utf-8") as f:
        resume_text = f.read()

    return await analyze_resume(resume_text, vacancy_text, market, level)
