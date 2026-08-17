"""Low-level helpers for HH chat browser workflows."""

import logging
import re


log = logging.getLogger("chat_responder")
CHATIK_ROOT = "https://chatik.hh.ru"
CHATIK_NAVIGATION_ATTEMPTS = 2
CHATIK_NAVIGATION_TIMEOUT_MS = 20000
CHATIK_READY_TIMEOUT_MS = 12000
CHATIK_CHAT_READY_SELECTOR = (
    '[data-qa^="chatik-chat-message-"], '
    'textarea[data-qa="chatik-new-message-text"]'
)


def normalize_sent_message_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\xa0", " ")).strip()


def message_matches_sent_text(
    actual: str,
    expected: str,
    *,
    normalize_text=normalize_sent_message_text,
) -> bool:
    actual_norm = normalize_text(actual)
    expected_norm = normalize_text(expected)
    if not actual_norm or not expected_norm:
        return False
    prefix_len = min(len(expected_norm), max(20, len(expected_norm) // 2))
    return actual_norm.startswith(expected_norm[:prefix_len])


def messages_contain_sent_text(
    messages: list[dict],
    expected: str,
    *,
    message_matches=message_matches_sent_text,
) -> bool:
    return any(
        message.get("is_me")
        and message_matches(message.get("text") or "", expected)
        for message in messages[-8:]
    )


def quick_reply_choice(
    text: str,
    *,
    normalize_text=normalize_sent_message_text,
) -> str:
    match = re.match(
        r"^(да|нет)(?:[\s,.:;!?—-]|$)",
        normalize_text(text),
        re.I,
    )
    return match.group(1).capitalize() if match else ""


async def reset_page_after_navigation_failure(page, *, logger=log) -> None:
    """Cancel a stuck chatik navigation before opening the next chat."""
    try:
        await page.goto("about:blank", wait_until="commit", timeout=10000)
    except Exception as exc:
        logger.debug("failed to reset page after chatik navigation error: %s", exc)


async def open_chatik_page(
    page,
    url: str,
    ready_selector: str,
    *,
    settle_ms: int,
    attempts: int = CHATIK_NAVIGATION_ATTEMPTS,
    ready_timeout_ms: int = CHATIK_READY_TIMEOUT_MS,
    log_failures: bool = True,
    navigation_timeout_ms: int = CHATIK_NAVIGATION_TIMEOUT_MS,
    reset_page=reset_page_after_navigation_failure,
    logger=log,
) -> None:
    """Open a chatik page and retry once after resetting a stuck tab."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            # Chatik is an SPA; DOMContentLoaded can hang even after the useful UI
            # is rendered. Wait for the actual chatik element instead.
            await page.goto(
                url,
                wait_until="commit",
                timeout=navigation_timeout_ms,
            )
            await page.wait_for_selector(
                ready_selector,
                state="attached",
                timeout=ready_timeout_ms,
            )
            await page.wait_for_timeout(settle_ms)
            return
        except Exception as exc:
            last_exc = exc
            current_url = getattr(page, "url", "") or ""
            if "account/login" in current_url:
                raise RuntimeError(f"chatik redirected to HH login: {current_url}") from exc
            log_method = logger.warning if log_failures else logger.debug
            log_method(
                "chatik open attempt %d/%d failed for %s: %s",
                attempt,
                attempts,
                url,
                exc,
            )
            await reset_page(page)

    assert last_exc is not None
    raise last_exc
