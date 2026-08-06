from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlparse


FORM_URL_RE = re.compile(r"https?://[^\s<>'\")]+", re.I)


def _is_google_form_url(value: str) -> bool:
    low = unquote(str(value or "")).lower()
    return "docs.google.com/forms" in low or "forms.gle/" in low


def _strip_url_tail(value: str) -> str:
    return (value or "").strip().rstrip(".,;:!?)>]}\"'")


def normalize_google_form_url(raw: str) -> str:
    value = html.unescape(_strip_url_tail(str(raw or "")))
    if not value:
        return ""
    decoded = unquote(value)
    try:
        parsed = urlparse(decoded)
        params = parse_qs(parsed.query)
    except Exception:
        return decoded if _is_google_form_url(decoded) else ""
    for key in ("url", "u", "to", "target", "backurl", "redirect", "q"):
        for candidate in params.get(key, []):
            candidate = unquote(_strip_url_tail(candidate))
            if _is_google_form_url(candidate):
                return candidate
    if _is_google_form_url(decoded):
        return decoded
    return ""


def extract_google_form_urls(
    text: str = "",
    links: list[dict] | list[str] | None = None,
) -> list[str]:
    found: list[str] = []
    for match in FORM_URL_RE.finditer(text or ""):
        url = normalize_google_form_url(match.group(0))
        if url and url not in found:
            found.append(url)
    for item in links or []:
        candidates = [item.get("href") or "", item.get("text") or ""] if isinstance(item, dict) else [str(item)]
        for candidate in candidates:
            url = normalize_google_form_url(candidate)
            if url and url not in found:
                found.append(url)
    return found
