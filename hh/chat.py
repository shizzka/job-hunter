"""Low-level helpers for HH chat browser workflows."""

import logging
import re
from typing import Any

from chat_screening import classify_message_author

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


async def list_chats(
    page,
    *,
    chatik_root: str = CHATIK_ROOT,
    open_page=open_chatik_page,
) -> list[dict]:
    """Открыть chatik root и вернуть свежие чаты с metadata.

    Chatik виртуализует список: в DOM присутствует только видимое окно.
    Поэтому нельзя сначала проскроллить вниз, а потом читать DOM — так
    теряются свежие чаты из верхнего окна.
    """
    await open_page(
        page,
        f"{chatik_root}/",
        '[data-qa^="chatik-open-chat-"]',
        settle_ms=2000,
    )

    chats = await page.evaluate("""async () => {
        const all = [...document.querySelectorAll('*')];
        const scroller = all.filter(el => {
            const cs = getComputedStyle(el);
            return (cs.overflowY === 'auto' || cs.overflowY === 'scroll')
                && el.scrollHeight > el.clientHeight + 50;
        }).sort((a,b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight))[0];
        const seen = new Map();
        const collect = () => {
            const items = [...document.querySelectorAll('[data-qa^="chatik-open-chat-"]')];
            for (const el of items) {
                const qa = el.getAttribute('data-qa') || '';
                const idMatch = qa.match(/^chatik-open-chat-(\\d+)$/);
                if (!idMatch) continue;
                const id = idMatch[1];
                if (seen.has(id)) continue;
                const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                seen.set(id, {chat_id: id, preview: text.slice(0, 300)});
            }
        };

        collect();
        if (scroller) {
            scroller.scrollTop = 0;
            await new Promise(r => setTimeout(r, 500));
            collect();
            let last = -1;
            // Проверяем свежую верхнюю часть списка. Пустые записи новых откликов
            // позже отфильтруются без открытия страницы чата.
            for (let i = 0; i < 10; i++) {
                scroller.scrollTop += Math.max(320, Math.floor(scroller.clientHeight * 0.75));
                await new Promise(r => setTimeout(r, 250));
                collect();
                if (scroller.scrollTop === last) break;
                last = scroller.scrollTop;
            }
        }
        return [...seen.values()];
    }""")
    return chats


async def extract_messages(
    page,
    *,
    classify_author=classify_message_author,
) -> dict[str, Any]:
    """Read messages from the currently open chat without navigating."""
    data = await page.evaluate("""() => {
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
            // Avatar can be an img or an icon with aria-label (robot recruiter).
            const avatarImg = b.querySelector('img[alt]');
            const avatarLabelEl = b.querySelector(
                '[data-qa="chat-bubble-wrapper"] [aria-label]'
            );
            const avatarAlt = avatarImg
                ? (avatarImg.getAttribute('alt') || '')
                : (avatarLabelEl?.getAttribute('aria-label') || '');
            const avatarSrc = avatarImg ? avatarImg.src : '';
            // Incoming continuation bubbles may omit author/avatar. Outgoing CSS
            // markers are the reliable way to identify our messages.
            const is_me = Boolean(
                b.querySelector('[class*="chat-bubble_outgoing"]')
                || b.querySelector('[class*="message_my"]')
            );
            const links = [...b.querySelectorAll('a[href]')].map(a => ({
                href: a.href || '',
                text: (a.innerText || '').trim(),
            }));
            out.push({
                id: mid,
                text,
                author,
                avatar_alt: avatarAlt,
                avatar_src: avatarSrc,
                links,
                is_ai: false,
                is_me,
                is_other: !is_me,
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
    for message in data.get("messages", []):
        classify_author(message)
    return data


async def get_messages(
    page,
    chat_id: str,
    *,
    chatik_root: str = CHATIK_ROOT,
    ready_selector: str = CHATIK_CHAT_READY_SELECTOR,
    open_page=open_chatik_page,
    extract_current_messages=extract_messages,
) -> dict[str, Any]:
    """Открыть chat прямой URL, вернуть messages+vacancy."""
    url = f"{chatik_root}/chat/{chat_id}"
    await open_page(
        page,
        url,
        ready_selector,
        settle_ms=2500,
    )
    return await extract_current_messages(page)


async def get_messages_safe(
    page,
    chat_id: str,
    *,
    attempts: int = 1,
    ready_timeout_ms: int = 5000,
    settle_ms: int = 800,
    chatik_root: str = CHATIK_ROOT,
    ready_selector: str = CHATIK_CHAT_READY_SELECTOR,
    open_page=open_chatik_page,
    extract_current_messages=extract_messages,
    reset_page=reset_page_after_navigation_failure,
) -> dict[str, Any]:
    """Best-effort chat read for scanners that should skip broken/empty chats quickly."""
    url = f"{chatik_root}/chat/{chat_id}"
    try:
        await open_page(
            page,
            url,
            ready_selector,
            settle_ms=settle_ms,
            attempts=attempts,
            ready_timeout_ms=ready_timeout_ms,
            log_failures=False,
        )
        data = await extract_current_messages(page)
        data.setdefault("error", "")
        return data
    except Exception as exc:
        await reset_page(page)
        return {
            "messages": [],
            "vacancy": {},
            "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:180]}",
            "chat_id": str(chat_id),
        }
