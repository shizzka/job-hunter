"""Low-level helpers for HH chat browser workflows."""

import re


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
