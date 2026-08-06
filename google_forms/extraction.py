from __future__ import annotations

import re


async def extract_form_questions(page) -> list[dict]:
    await page.wait_for_selector('form, div[role="listitem"]:visible', timeout=30000)
    questions = await page.evaluate("""() => {
        const clean = (value) => String(value || '').replace(/\\u00a0/g, ' ').replace(/[ \\t]+/g, ' ').trim();
        const unique = (values) => [...new Set(values.map(clean).filter(Boolean))];
        const isVisible = (el) => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        };
        const items = [...document.querySelectorAll('div[role="listitem"]')].filter(isVisible);
        const out = [];
        for (let idx = 0; idx < items.length; idx++) {
            const item = items[idx];
            const radios = [...item.querySelectorAll('[role="radio"]')];
            const checks = [...item.querySelectorAll('[role="checkbox"]')];
            const textFields = [...item.querySelectorAll('textarea, input[type="text"], input[type="email"], input[type="number"], input[type="url"], input[type="tel"]')]
                .filter(el => !el.disabled && el.type !== 'hidden');
            const listboxes = [...item.querySelectorAll('[role="listbox"]')];
            if (!radios.length && !checks.length && !textFields.length && !listboxes.length) continue;

            const heading = item.querySelector('[role="heading"], .M7eMe');
            let question = clean(heading ? heading.innerText : '');
            const optionLabels = unique([...radios, ...checks].map(el => el.getAttribute('aria-label') || el.innerText));
            if (!question) {
                const lines = clean(item.innerText).split('\\n').map(clean).filter(Boolean);
                question = lines.find(line => line !== '*' && !optionLabels.includes(line) && !/обязательный вопрос/i.test(line)) || '';
            }
            let type = 'text';
            if (radios.length) type = 'radio';
            else if (checks.length) type = 'checkbox';
            else if (listboxes.length) type = 'select';
            const requiredHints = unique([...item.querySelectorAll('[aria-label], [data-tooltip], [title]')]
                .map(el => [el.getAttribute('aria-label'), el.getAttribute('data-tooltip'), el.getAttribute('title')].join(' ')));
            const requiredStar = [...item.querySelectorAll('span, div')].some(el => {
                const cls = String((el.className && el.className.baseVal) || el.className || '');
                const label = el.getAttribute('aria-label') || '';
                return clean(el.innerText) === '*' && /vnumgf|required|обяз/i.test(`${cls} ${label}`);
            });
            const required = /обязательный вопрос|required/i.test(`${item.innerText || ''} ${requiredHints.join(' ')}`) || requiredStar;
            out.push({
                index: out.length,
                dom_index: idx,
                question: question.replace(/\\s+\\*$/, ''),
                type,
                required,
                options: optionLabels,
            });
        }
        return out;
    }""")
    return [
        question
        for question in questions
        if (question.get("question") or "").strip()
        and not _looks_like_form_info_block(question.get("question") or "")
    ]


def _looks_like_form_info_block(question_text: str) -> bool:
    text = re.sub(r"\s+", " ", str(question_text or "").casefold()).strip()
    if not text:
        return False
    return (
        text.startswith("благодарим за заполнение анкеты")
        or ("мы внимательно рассматриваем каждую анкету" in text and "спасибо за уделенное время" in text)
        or ("если вы не получили ответ" in text and "это будет означать" in text)
    )
