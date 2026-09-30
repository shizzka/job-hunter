"""Confirmed candidate facts and adaptive Telegram interview plans."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from typing import Any

import config
from llm_client import get_llm_client
from llm_utils import parse_llm_json


def _profile_dir() -> str:
    return os.path.dirname(config.RESUME_FILE) or os.path.expanduser("~/.job-hunter")


def data_path(profile_dir: str | None = None) -> str:
    return os.path.join(profile_dir or _profile_dir(), "candidate_interview.json")


def load(profile_dir: str | None = None) -> dict[str, Any]:
    try:
        with open(data_path(profile_dir), encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    return {"facts": []}


def _save(data: dict[str, Any], profile_dir: str | None = None) -> None:
    path = data_path(profile_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="candidate-interview-", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def facts(profile_dir: str | None = None) -> list[dict[str, str]]:
    value = load(profile_dir).get("facts") or []
    return [item for item in value if isinstance(item, dict) and item.get("text")]


def add_fact(text: str, *, topic: str = "общий", profile_dir: str | None = None, max_chars: int | None = 1200) -> dict[str, str]:
    clean = " ".join(str(text or "").split()).strip()
    if not clean:
        raise ValueError("Пустой факт нельзя сохранить")
    if max_chars is not None and len(clean) > max_chars:
        raise ValueError(f"Факт слишком длинный: максимум {max_chars} символов")
    data = load(profile_dir)
    current = [item for item in data.get("facts") or [] if isinstance(item, dict)]
    item = {
        "text": clean,
        "topic": " ".join(str(topic or "общий").split())[:80] or "общий",
        "source": "telegram_confirmed",
        "confirmed_at": datetime.now().isoformat(timespec="seconds"),
    }
    if not any(existing.get("text", "").casefold() == clean.casefold() for existing in current):
        current.append(item)
        data["facts"] = current[-100:]
        _save(data, profile_dir)
    return item


def prompt_block(limit_chars: int = 3000, profile_dir: str | None = None) -> str:
    confirmed = facts(profile_dir)
    if not confirmed:
        return ""
    lines = [
        "ПОДТВЕРЖДЁННЫЕ ПОЛЬЗОВАТЕЛЕМ ФАКТЫ (приоритет над выводами из резюме):",
        "Используй только в указанной точности; не усиливай уровень навыка и не переносись в другую профессию.",
    ]
    lines.extend(f"- [{item.get('topic', 'общий')}] {item['text']}" for item in confirmed)
    result = "\n".join(lines) + "\n\n"
    return result if len(result) <= limit_chars else result[:limit_chars - 1].rstrip() + "…\n\n"


def _fallback_plan(resume_text: str) -> dict[str, Any]:
    headline = next((line.removeprefix("#").strip() for line in resume_text.splitlines() if line.strip()), "текущего направления")
    return {
        "profile_hypothesis": headline[:140],
        "questions": [
            {"topic": "целевая роль", "question": "Какие должности вы хотите рассматривать в первую очередь?", "answer_hint": "Например: электромонтажник, QA Engineer, менеджер по продажам."},
            {"topic": "последний опыт", "question": "Что вы делали сами на последнем месте работы: 2–4 конкретные задачи?", "answer_hint": "Только то, что сможете уверенно объяснить работодателю."},
            {"topic": "инструменты", "question": "Какими инструментами или оборудованием вы работали самостоятельно, а с чем только знакомы?", "answer_hint": "Разделите: использовал сам / помогал / видел или изучал."},
            {"topic": "границы", "question": "Какой опыт, уровень навыка или задачи нельзя заявлять работодателю?", "answer_hint": "Например: нет коммерческого опыта, не готов к ночным сменам, не AQA."},
            {"topic": "условия", "question": "Какие условия работы для вас обязательны или исключены?", "answer_hint": "Формат, город, график, зарплата, командировки."},
        ],
    }


async def build_plan(resume_text: str, *, target_role: str = "", current_facts: list[dict[str, str]] | None = None) -> dict[str, Any]:
    if not resume_text.strip():
        return _fallback_plan("")
    known = "\n".join(f"- {item.get('text', '')}" for item in (current_facts or [])[:30]) or "нет"
    prompt = f"""Составь адаптивный план короткого интервью для кандидата, который ищет работу.
Не считай, что это QA-профиль: выведи направление только из резюме и цели.
Нужны 5–8 вопросов, которые закроют пробелы и предотвратят выдумывание опыта в откликах.
Не спрашивай то, что уже явно есть в резюме или подтверждённых фактах. Не спрашивай контакты, дату рождения и другие личные данные.
Для каждого вопроса дай topic, question и answer_hint. Вопросы должны быть простыми и пригодными для Telegram.

Целевая роль, если задана: {target_role or 'не задана'}
Подтверждённые факты:\n{known}

Резюме:\n{resume_text[:12000]}

Верни только JSON: {{"profile_hypothesis":"...","questions":[{{"topic":"...","question":"...","answer_hint":"..."}}]}}"""
    try:
        response = await get_llm_client().chat.completions.create(
            model=config.HH_FACTS_EXTRACT_MODEL or config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}], temperature=0.2, max_tokens=1100,
        )
        parsed = parse_llm_json(response.choices[0].message.content or "")
        questions = parsed.get("questions") if isinstance(parsed, dict) else None
        if not isinstance(questions, list):
            raise ValueError("questions missing")
        normalized = []
        for item in questions[:8]:
            if not isinstance(item, dict):
                continue
            question = " ".join(str(item.get("question") or "").split()).strip()
            if not question:
                continue
            normalized.append({
                "topic": " ".join(str(item.get("topic") or "общий").split())[:80],
                "question": question[:700],
                "answer_hint": " ".join(str(item.get("answer_hint") or "").split())[:500],
            })
        if normalized:
            return {"profile_hypothesis": str(parsed.get("profile_hypothesis") or "").strip()[:200], "questions": normalized}
    except Exception:
        pass
    return _fallback_plan(resume_text)


def render_facts(limit: int = 12, profile_dir: str | None = None) -> str:
    current = facts(profile_dir)
    if not current:
        return "🧠 Подтверждённых фактов пока нет. Начните интервью или добавьте факт своими словами."
    lines = ["🧠 Подтверждённые факты", ""]
    for index, item in enumerate(current[-limit:], start=max(1, len(current) - limit + 1)):
        lines.append(f"{index}. [{item.get('topic', 'общий')}] {item['text']}")
    lines.append("\nЭти факты используются в письмах и ответах на анкеты с приоритетом над догадками ИИ.")
    return "\n".join(lines)
