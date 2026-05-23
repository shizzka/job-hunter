"""Бенчмарк 6 Ollama-моделей на 4 LLM-задачах jobhunter'a.

Задачи:
  1. matcher.evaluate_vacancy — оценка релевантности (JSON: score/reason/red_flags)
  2. matcher.generate_cover_letter — творческий текст сопровода
  3. hh_client._answer_question_with_llm — короткий ответ на свободный вопрос (JSON)
  4. facts.extract_facts_from_resume — извлечение фактов из резюме (JSON)

Для каждой пары (model, task) измеряем:
- latency
- JSON-валидность (для задач возвращающих JSON)
- длину output
- первые 200 символов output (для качественной оценки)

Запуск:
    cd /home/q/job-hunter && set -a; . ~/.job-hunter/job-hunter.env; set +a; \\
        HTTP_PROXY= HTTPS_PROXY= ./venv/bin/python scripts/smoke/model_bench.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

import config
from openai import AsyncOpenAI
from llm_utils import parse_llm_json

MODELS = [
    "gpt-oss:120b",
    "qwen3-coder:480b",
    "glm-4.6",
    "minimax-m2",
    "cogito-2.1:671b",
    "nemotron-3-super",
]

# ── Fixtures ────────────────────────────────────────────────────────────────

VACANCY = {
    "title": "Middle QA Engineer (Manual)",
    "company": "Acme Tech",
    "salary": "не указана",
    "snippet": "Ручное тестирование REST API, web-приложений. Опыт от 1 года.",
}
VACANCY_DETAILS = (
    "Требования: 1+ год опыта в ручном тестировании, Postman, навыки SQL, "
    "понимание HTTP/REST, опыт с TestRail или TestIT, базовый английский. "
    "Условия: офис в СПб (гибрид), полный день, ДМС, ЗП от 120000 на руки."
)

RESUME = """## QA Engineer

Кандидат: Eugene
Город: Санкт-Петербург

## Опыт работы
2025 — настоящее время · QA Engineer, ООО «Софт»
- Ручное функциональное тестирование веб-приложения (личный кабинет, биллинг).
- REST API через Postman, проверка JSON-ответов, авторизация.
- TestIT: создаю и поддерживаю ~360 тест-кейсов.
- Баг-репорты в YouTrack, сетевая диагностика через DevTools и Charles.

До 2025 (13+ лет) — Инженер по технической диагностике
- Диагностика и обслуживание промышленной/бытовой техники.
- Не QA, но привычка докапываться до причины.

## Навыки
- Manual QA, функциональное и интеграционное тестирование
- REST API, JSON, HTTP-методы
- Postman, TestIT, YouTrack, Charles, DevTools
- Базовый SQL
- Английский — не указан
"""

FREE_TEXT_QUESTION = {
    "question_text": "Расскажите кратко почему вы подходите на эту вакансию",
    "input_type": "textarea",
    "max_length": 500,
}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _new_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY or "no-key",
    )


async def _call(model: str, messages: list[dict], temperature: float = 0.3, max_tokens: int = 800) -> tuple[str, float, str | None]:
    """Возвращает (raw_text, elapsed_s, error|None)."""
    t0 = time.time()
    try:
        client = _new_client()
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        raw = (resp.choices[0].message.content or "").strip()
        return raw, time.time() - t0, None
    except Exception as exc:
        return "", time.time() - t0, str(exc)[:200]


# ── Tasks ────────────────────────────────────────────────────────────────────

EVAL_PROMPT = f"""Ты — ассистент по поиску работы. Оцени подходит ли вакансия для кандидата.

## Резюме кандидата:
{RESUME}

## Вакансия:
Название: {VACANCY['title']}
Компания: {VACANCY['company']}
Зарплата: {VACANCY['salary']}
Краткое описание: {VACANCY['snippet']}

## Полное описание вакансии:
{VACANCY_DETAILS}

## Задача:
Оцени по шкале 0-100 (>=60 — стоит откликнуться).

Верни ТОЛЬКО валидный JSON без markdown. Первый символ `{{`, последний `}}`. Формат:
{{"score": <0-100>, "reason": "<1-2 предложения>", "should_apply": <bool>, "red_flags": [<строки>]}}"""

COVER_PROMPT = f"""Ты — ассистент по поиску работы. Напиши короткое сопроводительное.

## Резюме кандидата:
{RESUME}

## Вакансия:
Название: {VACANCY['title']}
Компания: {VACANCY['company']}
Описание: {VACANCY_DETAILS}

## Длина и форма:
- 3-4 предложения, до 800 символов
- От первого лица, разговорный профессиональный тон
- Не начинать с "Я — QA"
- Не использовать "активно", "регулярно", "успешно"
- НЕ выдумывать инструменты которых нет в резюме (Java, Selenium, k6)

Напиши ТОЛЬКО текст письма, без заголовков."""

QUESTION_PROMPT = f"""Ты отвечаешь на вопрос работодателя на hh.ru.

Вопрос: {FREE_TEXT_QUESTION['question_text']}
Максимум символов: {FREE_TEXT_QUESTION['max_length']}

Резюме:
{RESUME}

Верни ТОЛЬКО JSON. Первый символ `{{`, последний `}}`:
{{"status": "answer" | "skip", "answer": "<текст>"}}"""

FACTS_PROMPT = f"""Извлеки из резюме структурированные факты в JSON.

Только то что явно есть в резюме. Не выдумывай. Если факт отсутствует — не включай поле.

Рекомендованные поля: location, willing_remote, willing_business_trips,
english_level, experience_years_total, experience_years_qa,
tools_used (list), tools_not_used (list), current_position, summary.

Резюме:
{RESUME}

Верни ТОЛЬКО валидный JSON. Первый символ `{{`, последний `}}`."""


TASKS = [
    ("1.evaluate", EVAL_PROMPT, True, 800, 0.3),     # JSON required
    ("2.cover_letter", COVER_PROMPT, False, 600, 0.3),
    ("3.question_free_text", QUESTION_PROMPT, True, 400, 0.1),
    ("4.facts_extract", FACTS_PROMPT, True, 1200, 0.1),
]


async def main():
    sysmsg = {"role": "system", "content": "Ты отвечаешь строго в формате как просит пользователь. Если просят JSON — никакого текста до или после JSON-объекта."}

    grid = {}  # (model, task) -> dict
    for task_name, prompt, json_required, max_tokens, temperature in TASKS:
        print(f"\n=== TASK: {task_name} ===")
        for model in MODELS:
            messages = [sysmsg, {"role": "user", "content": prompt}]
            raw, elapsed, err = await _call(model, messages, temperature=temperature, max_tokens=max_tokens)
            row = {
                "elapsed_s": round(elapsed, 1),
                "len": len(raw),
                "err": err,
                "json_ok": None,
                "json_parsed": None,
                "sample": raw[:220].replace("\n", " "),
            }
            if json_required and not err:
                try:
                    parsed = parse_llm_json(raw)
                    row["json_ok"] = True
                    row["json_parsed"] = parsed
                except Exception as exc:
                    row["json_ok"] = False
                    row["json_parse_err"] = str(exc)[:100]
            grid[(model, task_name)] = row
            mark = "✓" if (not err and (row["json_ok"] in (True, None))) else ("✗" if err else "JSON✗")
            print(f"  {model:<22} {mark}  {row['elapsed_s']:>5}s  len={row['len']:<5} {row['sample'][:90]}")

    # Сводка
    print("\n\n" + "=" * 90)
    print("СВОДКА")
    print("=" * 90)
    for task_name, _, _, _, _ in TASKS:
        print(f"\n## {task_name}")
        ranked = []
        for model in MODELS:
            r = grid[(model, task_name)]
            score = 0
            if r["err"]:
                score = -100
            elif r["json_ok"] is False:
                score = -50
            else:
                score = 100 - r["elapsed_s"]  # faster is better
            ranked.append((score, model, r))
        ranked.sort(reverse=True)
        for s, model, r in ranked:
            status = "OK" if not r["err"] and r["json_ok"] is not False else ("ERR" if r["err"] else "JSON-fail")
            print(f"  {model:<22} {status:<10} {r['elapsed_s']}s  len={r['len']}")

    # JSON-sample dump для дальнейшей ручной оценки
    out_path = "/tmp/model_bench_results.json"
    serializable_grid = {}
    for (model, task), row in grid.items():
        key = f"{task}__{model}"
        # уберём json_parsed из дампа (нечитабельно в большом объёме)
        slim = {k: v for k, v in row.items() if k != "json_parsed"}
        serializable_grid[key] = slim
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(serializable_grid, f, ensure_ascii=False, indent=2)
    print(f"\n📁 Детальный результат: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
