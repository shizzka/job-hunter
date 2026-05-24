"""Авто-ответы на сообщения AI-помощника hh.ru в чатах с работодателями.

AI-помощник hh.ru (chatik) — это системный бот, который после отклика
задаёт квалифицирующие вопросы кандидату. Маркер: <img alt="ИИ-помощник">
с уникальным asset src=https://hhcdn.ru/file/18274603.png.

Поток:
  1. list_chats_with_ai() — открыть chatik, проскроллить, найти чаты с AI
  2. для каждого: get_messages() — получить ленту сообщений
  3. фильтр: последнее сообщение от ИИ, не от нас, не отвечено ранее
  4. generate_answer() через LLM на основе резюме + facts + истории диалога
  5. send_message() или DRY-RUN preview в TG (по флагу HH_CHAT_AUTOSEND)

Зависит от HHClient (cookies/playwright) + notifier + matcher для LLM.

CLI: ./run.sh chat-respond  (=> agent.py --chat-respond)
ENV:
  HH_CHAT_RESPONDER_ENABLED=1
  HH_CHAT_AUTOSEND=0           # 0 — dry-run + скрин в TG, 1 — реальная отправка
  HH_CHAT_MAX_REPLIES_PER_CHAT=5  # safety lock
  HH_CHAT_RESPONDER_MODEL=cogito-2.1:671b  # пустое → fallback на LLM_MODEL
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from typing import Any

from openai import AsyncOpenAI

import config
import proxy_utils
from llm_utils import parse_llm_json

log = logging.getLogger("chat_responder")

CHATIK_ROOT = "https://chatik.hh.ru"
AI_ASSISTANT_AVATAR_URL = "https://hhcdn.ru/file/18274603.png"  # уникальный asset бота
AI_NAME = "ИИ-помощник"

STATE_FILENAME = "chat_responder_state.json"


# ── State ───────────────────────────────────────────────────────────────────

def _state_path() -> str:
    home = os.path.dirname(config.RESUME_FILE) or os.path.expanduser("~/.job-hunter")
    return os.path.join(home, STATE_FILENAME)


def load_state() -> dict:
    p = _state_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        log.warning("state read failed: %s", exc)
    return {}


def save_state(state: dict) -> None:
    p = _state_path()
    tmp = f"{p}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


# ── Chat listing ────────────────────────────────────────────────────────────

async def list_chats(page) -> list[dict]:
    """Открыть chatik root, скроллить весь список, вернуть список чатов с metadata."""
    await page.goto(f"{CHATIK_ROOT}/", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(4000)

    # Скроллим список до конца
    await page.evaluate("""async () => {
        const all = [...document.querySelectorAll('*')];
        const scroller = all.filter(el => {
            const cs = getComputedStyle(el);
            return (cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                && el.scrollHeight > el.clientHeight + 50;
        }).sort((a,b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];
        if (!scroller) return;
        let last = -1;
        for (let i = 0; i < 60; i++) {
            scroller.scrollTop += 800;
            if (scroller.scrollTop === last) break;
            last = scroller.scrollTop;
            await new Promise(r => setTimeout(r, 200));
        }
    }""")
    await page.wait_for_timeout(1000)

    chats = await page.evaluate("""() => {
        const items = [...document.querySelectorAll('[data-qa^="chatik-open-chat-"]')];
        return items.map(el => {
            const qa = el.getAttribute('data-qa') || '';
            const idMatch = qa.match(/^chatik-open-chat-(\\d+)$/);
            if (!idMatch) return null;
            const id = idMatch[1];
            const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            // text формат: 'Title TIME Company Last-msg-preview'
            return {chat_id: id, preview: text.slice(0, 300)};
        }).filter(Boolean);
    }""")
    return chats


# ── Message extraction ──────────────────────────────────────────────────────

async def get_messages(page, chat_id: str) -> list[dict]:
    """Открыть chat прямой URL, вернуть список сообщений сверху-вниз."""
    url = f"{CHATIK_ROOT}/chat/{chat_id}"
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3500)

    msgs = await page.evaluate("""() => {
        const ai_avatar = 'https://hhcdn.ru/file/18274603.png';
        const ai_name = 'ИИ-помощник';
        // основные bubbles
        const bubbles = [...document.querySelectorAll('[data-qa^="chatik-chat-message-"]')]
            .filter(el => /^chatik-chat-message-\\d+$/.test(el.getAttribute('data-qa') || ''));
        const out = [];
        for (const b of bubbles) {
            const qa = b.getAttribute('data-qa');
            const idMatch = qa.match(/^chatik-chat-message-(\\d+)$/);
            if (!idMatch) continue;
            const mid = idMatch[1];
            // text
            const textEl = b.querySelector('[data-qa$="-text"]')
                       || b.querySelector('[data-qa="chatik-chat-message-' + mid + '-text"]');
            const text = textEl ? (textEl.innerText || '').trim() : '';
            // author label inside the bubble
            const authorEl = b.querySelector('[data-qa="chat-bubble-author-name"]');
            const author = authorEl ? authorEl.innerText.trim() : '';
            // avatar inside the bubble
            const avatarImg = b.querySelector('img[alt]');
            const avatarAlt = avatarImg ? (avatarImg.getAttribute('alt') || '') : '';
            const avatarSrc = avatarImg ? avatarImg.src : '';
            // эвристика "моё" vs "их": у моих сообщений нет аватарки + название author'a
            const cls = (b.className || '').toString();
            const is_ai = avatarAlt === ai_name || avatarSrc === ai_avatar || author === ai_name;
            // moy: ни AI, ни employer-аватарки.  Точная эвристика: класс с 'own' / выравнивание справа.
            // hh.ru использует флекс выравнивание; зависит от вёрстки. Простой proxy: bubble без аватарки и
            // без author label — это own.
            const is_me = !avatarImg && !author;
            out.push({
                id: mid,
                text,
                author,
                avatar_alt: avatarAlt,
                avatar_src: avatarSrc,
                is_ai,
                is_me,
                is_other: !is_ai && !is_me,
            });
        }
        // page title / chat header — для extract названия вакансии и компании
        const headerCompany = (document.querySelector('[data-qa="chat-header-title"]')
                            || document.querySelector('header h1, h1'))?.innerText || '';
        const vacancyLink = document.querySelector('a[href*="/vacancy/"]');
        const vacancy = {
            title: vacancyLink ? vacancyLink.innerText.trim() : '',
            url: vacancyLink ? vacancyLink.href : '',
            company: headerCompany.trim(),
        };
        return {messages: out, vacancy};
    }""")
    return msgs  # {messages, vacancy}


# ── LLM ─────────────────────────────────────────────────────────────────────

_llm_client: AsyncOpenAI | None = None


def _get_llm_client() -> AsyncOpenAI:
    global _llm_client
    if _llm_client is None:
        _llm_client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY or "no-key",
            http_client=proxy_utils.llm_http_client(),
        )
    return _llm_client


def _format_dialog(messages: list[dict], take_last: int = 10) -> str:
    tail = messages[-take_last:]
    lines = []
    for m in tail:
        if m.get("is_ai"):
            tag = "AI"
        elif m.get("is_me"):
            tag = "Я"
        else:
            tag = m.get("author") or "Other"
        text = (m.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        lines.append(f"[{tag}] {text}")
    return "\n".join(lines)


async def generate_answer(
    messages: list[dict],
    vacancy: dict,
    resume_text: str,
) -> str | None:
    """Сгенерировать ответ на последний вопрос AI-помощника."""
    if not messages:
        return None
    last_ai = next((m for m in reversed(messages) if m.get("is_ai")), None)
    if not last_ai:
        return None
    question = last_ai.get("text", "").strip()
    if not question:
        return None

    # Контексты
    from prompt_blocks import (
        build_profile_note_block,
        build_facts_block,
        build_salary_rule_block,
    )
    profile_note = build_profile_note_block()
    facts = build_facts_block()
    salary = build_salary_rule_block()

    vacancy_block = ""
    if vacancy.get("title") or vacancy.get("company"):
        vacancy_block = (
            f"Контекст вакансии:\n"
            f"- Должность: {vacancy.get('title', '—')}\n"
            f"- Компания: {vacancy.get('company', '—')}\n\n"
        )

    dialog_block = _format_dialog(messages, take_last=10)

    prompt = f"""Ты отвечаешь в чате hh.ru от лица кандидата на вопрос ИИ-помощника работодателя.

Этот ИИ не примет уход от ответа, отшучивания, переспрашивания. Он re-ask'нет тот же вопрос. Отвечай ПО ДЕЛУ, фактически.

Опирайся на канонический профиль, структурированные факты, резюме и контекст вакансии. Если факт отсутствует — отвечай ЧЕСТНО: «нет такого опыта», «не работал с этим», «изучаю сейчас». Не выдумывай инструменты/языки/опыт.

Если AI спрашивает количество лет опыта в конкретной технологии — назови конкретно или скажи «не работал». QA-опыт у кандидата меньше года, не путай с общим инженерным.

Длина ответа: 2-4 предложения, конкретика. Без приветствий («Здравствуйте» — не нужно, мы уже в диалоге). Без шаблонных оборотов «активно», «успешно», «эффективно», «глубокий опыт».

{profile_note}{facts}{salary}{vacancy_block}История диалога (последние 10 реплик):
{dialog_block}

Текущий вопрос AI-помощника:
"{question}"

Резюме кандидата (для деталей):
{resume_text[:4000]}

Верни ТОЛЬКО валидный JSON, без markdown. Первый символ `{{`, последний `}}`. Формат:
{{
  "status": "answer" | "skip",
  "answer": "<текст ответа на вопрос AI>"
}}

Правила:
- status=skip только если совсем нельзя ответить (например AI пишет про оффер/зарплату/собеседование — это требует решения человека).
- НЕ начинай ответ с приветствия.
- НЕ объясняй что ты ассистент.
- Отвечай как Eugene в первом лице."""

    model = (getattr(config, "HH_CHAT_RESPONDER_MODEL", "") or "").strip() or config.LLM_MODEL
    try:
        client = _get_llm_client()
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Ты отвечаешь строго JSON. Никакого текста до или после JSON-объекта."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=600,
        )
        raw = (resp.choices[0].message.content or "").strip()
        parsed = parse_llm_json(raw)
    except Exception as exc:
        log.warning("LLM chat-answer failed: %s", exc)
        return None

    if parsed.get("status") != "answer":
        return None
    answer = str(parsed.get("answer", "")).strip()
    return answer or None


# ── Send ────────────────────────────────────────────────────────────────────

async def _dismiss_cookies_banner(page) -> None:
    """Закрыть баннер «Мы используем файлы cookie» если есть — чтобы не перекрывал скрин."""
    for sel in (
        '[data-qa="cookies-policy-informer-accept"]',
        '[data-qa="cookies-policy-banner-accept"]',
        'button:has-text("Понятно")',
    ):
        try:
            btn = await page.query_selector(sel)
        except Exception:
            btn = None
        if btn:
            try:
                await btn.click()
                await page.wait_for_timeout(400)
                return
            except Exception:
                continue


async def fill_and_preview(page, chat_id: str, text: str) -> dict:
    """Перейти в chat, набрать текст в input. НЕ отправлять.
    Возвращает {filled, screenshot_path}."""
    url = f"{CHATIK_ROOT}/chat/{chat_id}"
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)
    await _dismiss_cookies_banner(page)
    inp = await page.query_selector('textarea[data-qa="chatik-new-message-text"]')
    if not inp:
        return {"filled": False, "reason": "input not found"}
    await inp.focus()
    await inp.fill(text)
    await page.wait_for_timeout(500)
    shot_path = os.path.join(config.HH_STATE_DIR, f"chat_preview_{chat_id}_{int(time.time())}.png")
    try:
        await page.screenshot(path=shot_path)
    except Exception:
        shot_path = ""
    return {"filled": True, "screenshot_path": shot_path}


async def send_message(page, chat_id: str, text: str) -> bool:
    """Полная отправка: перейти, набрать, нажать Send."""
    result = await fill_and_preview(page, chat_id, text)
    if not result.get("filled"):
        return False
    btn = await page.query_selector('[data-qa="chatik-do-send-message"]')
    if not btn:
        log.warning("send button not found")
        return False
    try:
        await btn.click()
        await page.wait_for_timeout(2000)
        return True
    except Exception as exc:
        log.warning("send click failed: %s", exc)
        return False


# ── Main process loop ──────────────────────────────────────────────────────

async def process_all(hh_client, dry_run: bool | None = None, max_replies_per_chat: int | None = None) -> dict:
    """Main entry: polling + reply.

    dry_run: если None — берётся из env HH_CHAT_AUTOSEND (0 = dry-run).
    """
    if dry_run is None:
        dry_run = not bool(int(os.getenv("HH_CHAT_AUTOSEND", "0") or 0))
    max_replies = max_replies_per_chat or int(os.getenv("HH_CHAT_MAX_REPLIES_PER_CHAT", "5"))

    if not hh_client._page:
        await hh_client.start(headless=True)
    page = hh_client._page

    # Загрузить cookies через домашнюю страницу hh.ru
    await page.goto("https://hh.ru/", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1500)

    state = load_state()
    summary = {"chats_scanned": 0, "with_ai": 0, "answers_drafted": 0, "answers_sent": 0, "skipped": 0, "details": []}

    chats = await list_chats(page)
    summary["chats_scanned"] = len(chats)
    log.info("found %d chats total", len(chats))

    try:
        from hh_client import _load_resume_text  # late import
    except Exception:
        _load_resume_text = lambda: ""
    resume_text = _load_resume_text()

    try:
        import notifier
    except Exception:
        notifier = None

    for chat in chats:
        chat_id = chat["chat_id"]
        chat_state = state.setdefault(chat_id, {})
        replies_so_far = int(chat_state.get("replies_count", 0))
        if replies_so_far >= max_replies:
            log.info("chat %s: max replies (%d) reached, skip", chat_id, max_replies)
            summary["skipped"] += 1
            continue

        try:
            data = await get_messages(page, chat_id)
        except Exception as exc:
            log.warning("get_messages(%s) failed: %s", chat_id, exc)
            continue
        msgs = data.get("messages", [])
        if not any(m.get("is_ai") for m in msgs):
            continue
        summary["with_ai"] += 1

        # последнее сообщение — оно должно быть от AI и НЕ от нас (иначе уже отвечено)
        last = msgs[-1] if msgs else None
        if not last or last.get("is_me"):
            continue
        if not last.get("is_ai"):
            continue  # последний от человека-HR — не лезем
        last_id = last.get("id")
        if chat_state.get("last_replied_msg_id") == last_id:
            continue  # уже отвечали на это сообщение

        # генерируем ответ
        answer = await generate_answer(msgs, data.get("vacancy", {}), resume_text)
        if not answer:
            log.info("chat %s: LLM не дал ответ", chat_id)
            summary["skipped"] += 1
            continue
        summary["answers_drafted"] += 1

        vac = data.get("vacancy", {})
        detail = {
            "chat_id": chat_id,
            "vacancy": vac.get("title", ""),
            "company": vac.get("company", ""),
            "question": (last.get("text") or "")[:300],
            "answer": answer,
        }

        if dry_run:
            # Превью: набираем текст без отправки + скрин
            preview = await fill_and_preview(page, chat_id, answer)
            detail["dry_run"] = True
            detail["preview"] = preview
            if notifier:
                try:
                    caption = (
                        f"🤖 <b>Чат с AI-помощником (DRY-RUN)</b>\n"
                        f"📋 {vac.get('title', '—')} @ {vac.get('company', '—')}\n\n"
                        f"❓ <i>{(last.get('text') or '')[:400]}</i>\n\n"
                        f"💬 <b>Готов ответить:</b>\n{answer[:800]}\n\n"
                        f"Чтобы включить авто-отправку: <code>HH_CHAT_AUTOSEND=1</code>"
                    )
                    if preview.get("screenshot_path"):
                        await notifier.send_photo(preview["screenshot_path"], caption=caption)
                    else:
                        await notifier.send_message_with_markup(caption)
                except Exception as exc:
                    log.warning("notify dry-run failed: %s", exc)
        else:
            # реальная отправка
            ok = await send_message(page, chat_id, answer)
            detail["sent"] = ok
            if ok:
                summary["answers_sent"] += 1
                chat_state["last_replied_msg_id"] = last_id
                chat_state["replies_count"] = replies_so_far + 1
                chat_state["last_reply_at"] = time.time()
                save_state(state)
                if notifier:
                    try:
                        caption = (
                            f"🤖 <b>Ответил в чате</b>\n"
                            f"📋 {vac.get('title', '—')} @ {vac.get('company', '—')}\n\n"
                            f"❓ <i>{(last.get('text') or '')[:300]}</i>\n\n"
                            f"💬 {answer[:800]}\n\n"
                            f"<a href='https://chatik.hh.ru/chat/{chat_id}'>Открыть чат</a>"
                        )
                        await notifier.send_message_with_markup(caption)
                    except Exception as exc:
                        log.warning("notify sent failed: %s", exc)
            else:
                summary["skipped"] += 1

        summary["details"].append(detail)
        # cooldown между ответами в разных чатах
        await asyncio.sleep(int(os.getenv("HH_CHAT_REPLY_COOLDOWN_S", "30")))

    return summary
