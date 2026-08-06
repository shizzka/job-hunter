"""hh.ru text-captcha solver.

Два этапа:
1. Vision-LLM (qwen3-vl) распознаёт текст с картинки → сабмитим. До
   `HH_CAPTCHA_VISION_RETRIES` попыток (картинка обновляется кнопкой ↻ hh).
2. Эскалация в Telegram: скрин + ждём ответа человека через captcha_bridge.
   Окно `HH_CAPTCHA_HUMAN_WINDOW_S` сек. По таймауту — soft cooldown 15 мин
   и follow-up в TG с inline-кнопкой под stage: поиск или повтор входа HH.

Зависит от ``client`` — объекта типа HHClient с методами:
- ``client._page`` — playwright Page
- ``await client._detect_anti_bot_kind()`` → "" | "captcha" | "ddos_guard" | …
- ``await client._click_with_fallbacks(element, label)`` → bool
"""
from __future__ import annotations

import base64
import logging
import os
import re
import time
from typing import Any

import config

log = logging.getLogger("captcha_solver")


def _hh_auth_profile_from_stage(stage: str) -> str:
    parts = (stage or "").split(":")
    if len(parts) >= 2 and parts[0] == "hh_auth":
        profile = parts[1].strip()
        if re.fullmatch(r"[a-zA-Z0-9_.-]+", profile):
            return profile
    return ""


def _captcha_retry_markup(stage: str, request_id: str) -> dict:
    profile_name = _hh_auth_profile_from_stage(stage)
    if profile_name:
        return {
            "inline_keyboard": [[
                {"text": "🔐 Повторить вход HH", "callback_data": f"hh_reauth:{profile_name}"},
            ]],
        }
    return {
        "inline_keyboard": [[
            {"text": "🔁 Перезапустить поиск", "callback_data": f"captcha_retry:{request_id}"},
        ]],
    }


def _captcha_timeout_text(stage: str, total_attempts: int, captcha_timeout_s: int) -> str:
    if _hh_auth_profile_from_stage(stage):
        return (
            f"⚠️ Captcha #{total_attempts} не решена за {captcha_timeout_s // 60} мин — токен hh.ru истёк.\n"
            "Нажми «🔐 Повторить вход HH», когда сможешь — бот откроет свежую captcha."
        )
    return (
        f"⚠️ Captcha #{total_attempts} не решена за {captcha_timeout_s // 60} мин — токен hh.ru истёк.\n"
        "Нажми «🔁 Перезапустить поиск» когда сможешь — бот возьмёт свежую captcha."
    )


async def solve_captcha_with_vision_llm(screenshot_path: str, llm_client_factory) -> str | None:
    """Vision-LLM (qwen3-vl или подобный) распознаёт текст с captcha-картинки.

    ``llm_client_factory`` — callable, возвращающий настроенный AsyncOpenAI клиент
    (нужно чтобы вызывающий слой контролировал base_url/api_key/proxy).
    """
    model = (config.HH_CAPTCHA_VISION_MODEL or "").strip()
    if not model or not config.LLM_API_KEY:
        return None
    if not os.path.exists(screenshot_path):
        return None
    try:
        with open(screenshot_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
    except Exception as exc:
        log.warning("captcha vision read failed: %s", exc)
        return None

    prompt_text = (
        "Перед тобой captcha с hh.ru: серая картинка с искажённым текстом "
        "(одно или два русских слова, иногда сильно искажённых). "
        "Распознай ТОЛЬКО сам текст с этой картинки. "
        "Не добавляй комментарии, кавычки или знаки препинания. "
        "Если в тексте видна буква ё — пиши ё, если е — пиши е. "
        "Если текст одно слово — верни одно слово. "
        "Если два — раздели пробелом. Только текст."
    )
    try:
        client = llm_client_factory()
        resp = await client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
            temperature=0.1,
            max_tokens=60,
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        log.warning("vision LLM captcha failed: %s", exc)
        return None
    # Чистим: первая непустая строка, убираем кавычки/markdown
    for line in raw.splitlines():
        clean = line.strip().strip('"').strip("'").strip("`").strip()
        if clean:
            return clean[:100]
    return None


async def _find_captcha_input(page):
    for sel in (
        "input[placeholder*='Текст с картинки' i]",
        "input[name='captcha' i]",
        "input[name*='captcha' i]",
        "input[id*='captcha' i]:not([type='hidden'])",
    ):
        try:
            el = await page.query_selector(sel)
        except Exception:
            el = None
        if el:
            return el
    return None


async def _submit_answer(client: Any, answer: str) -> bool:
    page = client._page
    captcha_input = await _find_captcha_input(page)
    if not captcha_input:
        log.warning("captcha input not found, can't fill")
        return False
    try:
        await captcha_input.focus()
        await captcha_input.fill(answer)
        await page.wait_for_timeout(300)
    except Exception as exc:
        log.warning("captcha fill failed: %s", exc)
        return False
    for sel in (
        "button:has-text('Отправить')",
        "button:has-text('Подтвердить')",
        "button[type='submit']",
    ):
        try:
            btn = await page.query_selector(sel)
        except Exception:
            btn = None
        if btn and await client._click_with_fallbacks(btn, f"captcha_submit:{sel}"):
            await page.wait_for_timeout(2500)
            return True
    log.warning("captcha submit button not clicked")
    return False


async def _refresh_screenshot(page) -> str | None:
    try:
        path = os.path.join(config.HH_STATE_DIR, f"captcha_{int(time.time())}.png")
        await page.screenshot(path=path)
        return path
    except Exception as exc:
        log.warning("captcha screenshot failed: %s", exc)
        return None


async def try_solve_captcha_interactively(
    client: Any,
    llm_client_factory,
    stage: str = "",
    max_retries: int = 3,
) -> bool:
    """Решение текстовой captcha: vision-LLM → если не помогает, эскалация в TG."""
    try:
        import notifier
        import captcha_bridge
    except Exception as exc:
        log.warning("captcha-bridge unavailable: %s", exc)
        return False

    page = client._page
    vision_retries = int(config.HH_CAPTCHA_VISION_RETRIES or 0)
    total_attempts = 0

    while total_attempts < max_retries:
        kind = await client._detect_anti_bot_kind()
        if kind != "captcha":
            return kind == ""
        total_attempts += 1

        shot_path = await _refresh_screenshot(page)
        if not shot_path:
            return False

        # Этап 0: vision-LLM
        if vision_retries > 0:
            log.info("captcha attempt #%d via vision-LLM (%s)", total_attempts, config.HH_CAPTCHA_VISION_MODEL)
            answer = await solve_captcha_with_vision_llm(shot_path, llm_client_factory)
            if answer:
                log.info("vision-LLM proposed: %r", answer[:80])
                if await _submit_answer(client, answer):
                    kind_after = await client._detect_anti_bot_kind()
                    if kind_after != "captcha":
                        log.info("captcha solved by vision-LLM on attempt #%d", total_attempts)
                        return kind_after == ""
                    log.info("vision-LLM answer rejected, retry")
                    vision_retries -= 1
                    continue
            else:
                vision_retries -= 1
                if vision_retries > 0:
                    log.info("vision-LLM gave no answer, retry")
                    continue
                log.info("vision-LLM gave no answer, escalate to TG")

        # Этап 1: эскалация в TG.
        page_url = page.url if page else ""
        captcha_timeout_s = int(getattr(config, "HH_CAPTCHA_HUMAN_WINDOW_S", 300))
        request_id = captcha_bridge.create_request(shot_path, page_url=page_url, timeout_s=captcha_timeout_s)
        caption_parts = [
            f"🤖 hh.ru captcha (попытка {total_attempts}/{max_retries})",
            f"Stage: {stage}" if stage else "",
            f"Окно: {captcha_timeout_s // 60} мин (потом токен истечёт).",
            "Введи буквы с картинки — бот вставит в форму.",
            f"URL: {page_url}" if page_url else "",
        ]
        caption = "\n".join(p for p in caption_parts if p)
        retry_markup = _captcha_retry_markup(stage, request_id)
        try:
            await notifier.send_photo(shot_path, caption=caption, reply_markup=retry_markup)
        except Exception as exc:
            log.warning("captcha notify failed: %s", exc)

        log.info("waiting for captcha answer in TG (%d s)", captcha_timeout_s)
        answer = await captcha_bridge.wait_for_response(request_id, timeout_s=captcha_timeout_s, poll_interval_s=3.0)
        captcha_bridge.complete_request(request_id)
        if not answer:
            log.warning("captcha answer not received in time, applying soft cooldown + sending timeout notice")
            try:
                import hh_guard
                hh_guard.record_soft_cooldown(minutes=15, reason="captcha TG timeout")
            except Exception as exc:
                log.warning("soft cooldown record failed: %s", exc)
            try:
                timeout_text = _captcha_timeout_text(stage, total_attempts, captcha_timeout_s)
                await notifier.send_message_with_markup(timeout_text, reply_markup=retry_markup)
            except Exception as exc:
                log.warning("captcha timeout notify failed: %s", exc)
            return False

        if not await _submit_answer(client, answer):
            return False
        kind_after = await client._detect_anti_bot_kind()
        if kind_after != "captcha":
            log.info("captcha solved by TG-bridge on attempt #%d", total_attempts)
            return kind_after == ""
        log.info("TG answer rejected, retry")

    log.warning("captcha not solved after %d attempts", max_retries)
    return False


async def handle_anti_bot_with_solver(
    client: Any,
    llm_client_factory,
    kind: str,
    stage: str = "",
) -> str:
    """Если kind=='captcha' — пробуем решить через vision-LLM + TG-bridge.
    Возвращает kind после попытки ('' если решилось, иначе исходный kind)."""
    if kind != "captcha":
        return kind
    try:
        solved = await try_solve_captcha_interactively(client, llm_client_factory, stage=stage)
    except Exception as exc:
        log.warning("captcha solver exception: %s", exc)
        return kind
    if solved:
        try:
            fresh = await client._detect_anti_bot_kind()
        except Exception:
            fresh = ""
        return fresh
    return kind
