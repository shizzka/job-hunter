"""
Анализ резюме с помощью LLM.

Промт загружается из внешнего файла ~/.job-hunter/resume_prompt.md
(не коммитится в репозиторий).
"""
import logging
import os

from openai import AsyncOpenAI

import config
import proxy_utils

log = logging.getLogger("resume_analyzer")
PROMPT_FILE_NAME = "resume_prompt.md"
_client: AsyncOpenAI | None = None

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


def _profile_prompt_file() -> str:
    return os.path.join(config.JOB_HUNTER_HOME, PROMPT_FILE_NAME)


def _shared_prompt_file() -> str:
    home = os.path.abspath(config.JOB_HUNTER_HOME)
    parent = os.path.dirname(home)
    if os.path.basename(parent) != "profiles":
        return ""
    root_home = os.path.dirname(parent)
    return os.path.join(root_home, PROMPT_FILE_NAME)


def _prompt_candidates() -> list[str]:
    candidates = [_profile_prompt_file()]
    shared_prompt = _shared_prompt_file()
    if shared_prompt and shared_prompt not in candidates:
        candidates.append(shared_prompt)
    return candidates


def _load_prompt() -> tuple[str, str]:
    """
    Загрузить промт из файла.

    Формат файла: system-промт и user-промт разделены строкой '---'.
    Если разделителя нет — весь файл считается system-промтом,
    user-промт берётся fallback.

    Возвращает (system_prompt, user_prompt_template).
    """
    for prompt_file in _prompt_candidates():
        if not os.path.isfile(prompt_file):
            continue
        with open(prompt_file, encoding="utf-8") as f:
            content = f.read()

        if "\n---\n" in content:
            system_part, user_part = content.split("\n---\n", 1)
            return system_part.strip(), user_part.strip()

        # Весь файл — system prompt
        return content.strip(), _FALLBACK_USER

    profile_prompt_file = _profile_prompt_file()
    shared_prompt_file = _shared_prompt_file()
    searched = [profile_prompt_file]
    if shared_prompt_file:
        searched.append(shared_prompt_file)
    log.warning(
        "Файл промта не найден (%s) — используется базовый промт. "
        "Положи свой промт в этот файл для полного анализа.",
        ", ".join(searched),
    )
    return _FALLBACK_SYSTEM, _FALLBACK_USER


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY or "no-key",
            http_client=proxy_utils.llm_http_client(),
        )
    return _client


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
