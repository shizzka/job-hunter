"""Офлайн-проверка _answer_choice_with_llm на синтетических hh-вопросах.

Без браузера/hh.ru — только LLM, чтобы понять качество выбора и
найти баги в промпте до боевого прогона.

Запуск:
    cd /home/q/job-hunter && ./venv/bin/python tests/test_choice_prompt_offline.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config
from hh_client import HHClient


# Синтетические поля, скопированные с реальных hh-форм из логов 22-23 мая.
CASES = [
    {
        "name": "Кто вы по специальности (radio, реальный из лога 21:33)",
        "field": {
            "field_id": "f1",
            "control": "radio",
            "input_type": "radio",
            "group_name": "specialty",
            "question_text": "Кто из перечисленных лучше всего описывает вашу роль?",
            "options": [
                {"index": 0, "value": "load", "label": "Я - инженер по нагрузочному тестированию", "is_custom": False},
                {"index": 1, "value": "part", "label": "Нагрузочное тестирование было небольшой частью моих задач", "is_custom": False},
                {"index": 2, "value": "custom", "label": "Свой вариант", "is_custom": True},
            ],
        },
        "expectation": "должен выбрать index=1 (нагрузочное частью задач) или 2 (свой вариант с пояснением), потому что в резюме нагрузочное упоминается лишь вскользь",
    },
    {
        "name": "Готов к командировкам (radio Да/Нет)",
        "field": {
            "field_id": "f2",
            "control": "radio",
            "input_type": "radio",
            "group_name": "travel",
            "question_text": "Готовы ли вы к командировкам?",
            "options": [
                {"index": 0, "value": "yes", "label": "Да", "is_custom": False},
                {"index": 1, "value": "no", "label": "Нет", "is_custom": False},
            ],
        },
        "expectation": "выбор по контексту резюме; если в резюме нет markeров о готовности — допустимо skip",
    },
    {
        "name": "Уровень английского (radio с 4 опциями)",
        "field": {
            "field_id": "f3",
            "control": "radio",
            "input_type": "radio",
            "group_name": "english",
            "question_text": "Уровень английского языка",
            "options": [
                {"index": 0, "value": "a", "label": "A1-A2 (начальный/элементарный)", "is_custom": False},
                {"index": 1, "value": "b1", "label": "B1 (средний)", "is_custom": False},
                {"index": 2, "value": "b2", "label": "B2 (выше среднего)", "is_custom": False},
                {"index": 3, "value": "c", "label": "C1-C2 (продвинутый/свободный)", "is_custom": False},
            ],
        },
        "expectation": "выбор из резюме",
    },
    {
        "name": "Зарплатные ожидания (radio с custom)",
        "field": {
            "field_id": "f4",
            "control": "radio",
            "input_type": "radio",
            "group_name": "salary",
            "question_text": "Какие у вас зарплатные ожидания?",
            "options": [
                {"index": 0, "value": "skip", "label": "Я не готов указать свои ЗП ожидания", "is_custom": False},
                {"index": 1, "value": "specify", "label": "Я укажу свои ЗП ожидания (Укажите их в качестве своего варианта ответа)", "is_custom": True},
            ],
        },
        "expectation": "выбрать index=1 + custom_text с числом из резюме (типа 180000 или вилки)",
    },
    {
        "name": "Какие технологии используете (checkbox)",
        "field": {
            "field_id": "f5",
            "control": "checkbox",
            "input_type": "checkbox",
            "group_name": "tech",
            "question_text": "Какие из перечисленных инструментов используете в работе?",
            "options": [
                {"index": 0, "value": "postman", "label": "Postman", "is_custom": False},
                {"index": 1, "value": "testit", "label": "TestIT/TestRail", "is_custom": False},
                {"index": 2, "value": "youtrack", "label": "YouTrack/Jira", "is_custom": False},
                {"index": 3, "value": "selenium", "label": "Selenium WebDriver", "is_custom": False},
                {"index": 4, "value": "k6", "label": "k6/JMeter (нагрузочное)", "is_custom": False},
            ],
        },
        "expectation": "checkbox — несколько индексов; только те, что реально в резюме",
    },
    {
        "name": "Где живёте (select с 5 городами)",
        "field": {
            "field_id": "f6",
            "control": "select",
            "input_type": "select",
            "group_name": "city",
            "question_text": "Город проживания",
            "options": [
                {"index": 0, "value": "spb", "label": "Санкт-Петербург", "is_custom": False},
                {"index": 1, "value": "msk", "label": "Москва", "is_custom": False},
                {"index": 2, "value": "nn", "label": "Нижний Новгород", "is_custom": False},
                {"index": 3, "value": "other", "label": "Другой", "is_custom": True},
                {"index": 4, "value": "remote", "label": "Только удалённая работа", "is_custom": False},
            ],
        },
        "expectation": "город из резюме",
    },
]


VACANCY_CONTEXT = (
    "Должность: Middle QA Engineer (Manual)\n"
    "Компания: Acme Tech\n"
    "Описание: Тестирование backend и REST API web-приложения, опыт от 1 года, "
    "ручное тестирование, работа с Postman, TestIT, YouTrack, навыки SQL приветствуются."
)


async def main():
    resume_path = os.path.expanduser("~/.job-hunter/profiles/qa/resume.md")
    if not os.path.exists(resume_path):
        print(f"resume not found: {resume_path}")
        return
    resume_text = open(resume_path).read()

    client = HHClient()  # _page не нужен — мы дёргаем только LLM-метод
    print(f"Model: {config.LLM_MODEL}")
    print(f"Resume length: {len(resume_text)} chars\n")

    pass_count = 0
    fail_count = 0
    skip_count = 0

    for case in CASES:
        print("=" * 70)
        print(f"CASE: {case['name']}")
        print(f"Q: {case['field']['question_text']}")
        for o in case["field"]["options"]:
            mark = " [СВОЙ]" if o.get("is_custom") else ""
            print(f"  {o['index']}: {o['label']}{mark}")
        print(f"Expected: {case['expectation']}")
        t0 = time.time()
        try:
            result = await client._answer_choice_with_llm(
                case["field"], resume_text, page_text="", vacancy_context=VACANCY_CONTEXT
            )
        except Exception as e:
            print(f"EXCEPTION: {e}")
            fail_count += 1
            continue
        dt = time.time() - t0

        if result is None:
            print(f"RESULT: None (parse error or LLM glitch)  [{dt:.1f}s]")
            fail_count += 1
            continue
        if result.get("is_skip"):
            print(f"RESULT: SKIP  [{dt:.1f}s]")
            skip_count += 1
            continue

        sel = result.get("selected") or []
        for s in sel:
            idx = s["index"]
            label = case["field"]["options"][idx]["label"] if 0 <= idx < len(case["field"]["options"]) else "?"
            extra = f" + custom_text: {s['custom_text']!r}" if s.get("custom_text") else ""
            print(f"  PICKED: [{idx}] {label}{extra}")
        print(f"  [{dt:.1f}s]")
        pass_count += 1

    print("=" * 70)
    print(f"Summary: picked={pass_count}, skipped={skip_count}, failed={fail_count}, total={len(CASES)}")


if __name__ == "__main__":
    asyncio.run(main())
