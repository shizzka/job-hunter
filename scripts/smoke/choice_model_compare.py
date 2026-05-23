"""Сравнить две Ollama-модели на одинаковых choice-вопросах.

Запуск:
    cd /home/q/job-hunter && set -a; . ~/.job-hunter/job-hunter.env; set +a; \
        HTTP_PROXY= HTTPS_PROXY= ./venv/bin/python tests/test_choice_model_compare.py
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from hh_client import HHClient

# импортируем те же кейсы что в офлайн-тесте
from choice_prompt_offline import CASES, VACANCY_CONTEXT  # noqa: E402

MODELS = ["gpt-oss:120b", "qwen3-coder:480b"]


async def run_one_model(model: str, resume_text: str) -> dict:
    # переопределяем модель в config на лету
    orig = config.LLM_MODEL
    config.LLM_MODEL = model
    try:
        # очистим singleton клиента, чтобы он не закэшировал base_url+key
        import hh_client
        hh_client._question_answer_client = None  # noqa: SLF001

        client = HHClient()
        rows = []
        for case in CASES:
            t0 = time.time()
            try:
                result = await client._answer_choice_with_llm(
                    case["field"], resume_text, page_text="", vacancy_context=VACANCY_CONTEXT
                )
                err = None
            except Exception as e:
                result, err = None, str(e)
            dt = time.time() - t0

            row = {
                "case": case["name"][:50],
                "elapsed_s": round(dt, 1),
                "error": err,
            }
            if result is None:
                row["outcome"] = "FAIL"
            elif result.get("is_skip"):
                row["outcome"] = "SKIP"
            else:
                sel = result.get("selected") or []
                picks = []
                for s in sel:
                    idx = s["index"]
                    label = case["field"]["options"][idx]["label"] if 0 <= idx < len(case["field"]["options"]) else "?"
                    picks.append(f"[{idx}] {label[:40]}" + (f" + {s['custom_text'][:50]!r}" if s.get("custom_text") else ""))
                row["outcome"] = " | ".join(picks)
            rows.append(row)
        return {"model": model, "rows": rows}
    finally:
        config.LLM_MODEL = orig


async def main():
    resume_path = os.path.expanduser("~/.job-hunter/profiles/qa/resume.md")
    resume_text = open(resume_path).read()
    print(f"Resume: {len(resume_text)} chars\n")

    results = {}
    for model in MODELS:
        print(f"=== running {model} ===")
        results[model] = await run_one_model(model, resume_text)

    # сравнительная таблица
    print("\n" + "=" * 100)
    print(f"{'CASE':<50} | {MODELS[0]:<35} | {MODELS[1]:<35}")
    print("-" * 100)
    for i, case in enumerate(CASES):
        a = results[MODELS[0]]["rows"][i]
        b = results[MODELS[1]]["rows"][i]
        print(f"{case['name'][:48]:<50}")
        print(f"  {MODELS[0]:<32} ({a['elapsed_s']}s): {a['outcome'][:200]}")
        print(f"  {MODELS[1]:<32} ({b['elapsed_s']}s): {b['outcome'][:200]}")
        print()

    # сводка
    for m in MODELS:
        rows = results[m]["rows"]
        ok = sum(1 for r in rows if r["outcome"] not in ("FAIL", "SKIP"))
        skip = sum(1 for r in rows if r["outcome"] == "SKIP")
        fail = sum(1 for r in rows if r["outcome"] == "FAIL")
        total_time = sum(r["elapsed_s"] for r in rows)
        print(f"{m}: picked={ok}, skipped={skip}, failed={fail}, total_time={total_time:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
