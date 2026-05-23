"""Smoke test for _inspect_employer_questions on a saved HH question page snapshot.

Не входит в обычный pytest-прогон — нужен запущенный Chromium.
Запуск:
    cd /home/q/job-hunter && ./venv/bin/python tests/test_question_inspector_smoke.py
"""
import asyncio
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from playwright.async_api import async_playwright

from hh_client import HHClient

FIXTURE = os.path.join(_ROOT, "output_hh_question_debug.html")


async def main():
    assert os.path.exists(FIXTURE), f"fixture not found: {FIXTURE}"
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(f"file://{FIXTURE}")
        await page.wait_for_load_state("domcontentloaded")

        # Hand the page to a half-initialised HHClient just so we can call the method.
        client = HHClient()
        client._page = page

        result = await client._inspect_employer_questions()

        print("=== fields (count =", len(result.get("fields", [])), ") ===")
        for f in result.get("fields", []):
            shown = {
                k: v for k, v in f.items()
                if k in ("field_id", "control", "input_type", "group_name", "question_text", "options")
            }
            print(json.dumps(shown, ensure_ascii=False, indent=2)[:2000])
            print("-")

        print("=== unsupported_fields:", result.get("unsupported_fields"))
        print("=== unsupported_items:", result.get("unsupported_items"))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
