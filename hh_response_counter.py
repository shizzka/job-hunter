"""Exact HH response counters fetched from the authenticated negotiations pages."""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from pathlib import Path

import httpx

from state_store.json_store import JsonStore


SNAPSHOT_FILENAME = "hh_response_counter.json"
_NEGOTIATIONS_MODEL_RE = re.compile(
    r'"total"\s*:\s*(\d+)\s*,\s*"readOnlyInterval"\s*:\s*\d+'
    r'.{0,1000}?"pageCount"\s*:\s*"?([0-9]+)"?\s*,\s*'
    r'"filterInUse"\s*:\s*"([^"]+)"',
    re.DOTALL,
)
_COUNTERS_RE = re.compile(
    r'"applicantNegotiationsCounters"\s*:\s*\{.*?'
    r'"total"\s*:\s*\{([^{}]+)\}',
    re.DOTALL,
)


class HHResponseCounterError(RuntimeError):
    """Raised when HH counters cannot be fetched or parsed."""


def parse_negotiations_page(page_html: str, *, expected_filter: str) -> dict:
    decoded = html.unescape(page_html or "")
    models = [
        {
            "total": int(match.group(1)),
            "page_count": int(match.group(2)),
            "filter": match.group(3),
        }
        for match in _NEGOTIATIONS_MODEL_RE.finditer(decoded)
    ]
    model = next(
        (item for item in reversed(models) if item["filter"] == expected_filter),
        None,
    )
    if not model:
        raise HHResponseCounterError(
            f"HH response counter not found for filter {expected_filter!r}"
        )

    if expected_filter == "active":
        counters_match = _COUNTERS_RE.search(decoded)
        counters_text = counters_match.group(1) if counters_match else ""
        deleted_match = re.search(r'"deleted"\s*:\s*(\d+)', counters_text)
        if not deleted_match:
            raise HHResponseCounterError("HH deleted response counter not found")
        model["deleted"] = int(deleted_match.group(1))
    return model


def _load_playwright_cookies(cookies_file: str) -> list[dict]:
    try:
        with open(cookies_file, encoding="utf-8") as source:
            payload = json.load(source)
    except FileNotFoundError as exc:
        raise HHResponseCounterError(
            "Сессия HH не найдена. Сначала выполните «Вход HH»."
        ) from exc
    except Exception as exc:
        raise HHResponseCounterError(f"Не удалось прочитать сессию HH: {exc}") from exc

    cookies = payload.get("cookies", []) if isinstance(payload, dict) else payload
    if not isinstance(cookies, list) or not cookies:
        raise HHResponseCounterError(
            "Сессия HH пуста. Сначала выполните «Вход HH»."
        )
    return [item for item in cookies if isinstance(item, dict)]


def _authenticated_client(cookies_file: str) -> httpx.Client:
    client = httpx.Client(
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
        timeout=30,
    )
    for cookie in _load_playwright_cookies(cookies_file):
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name:
            continue
        client.cookies.set(
            name,
            value,
            domain=str(cookie.get("domain") or ".hh.ru"),
            path=str(cookie.get("path") or "/"),
        )
    return client


def _fetch_page(client: httpx.Client, url: str, *, expected_filter: str) -> dict:
    response = client.get(url)
    final_url = str(response.url)
    if "/account/login" in final_url or "/account/signup" in final_url:
        raise HHResponseCounterError(
            "Сессия HH истекла. Выполните «Вход HH» и повторите."
        )
    if response.status_code != 200:
        raise HHResponseCounterError(
            f"HH вернул HTTP {response.status_code} для счётчика откликов"
        )
    return parse_negotiations_page(
        response.text,
        expected_filter=expected_filter,
    )


def snapshot_path(home_dir: str) -> str:
    return os.fspath(Path(home_dir) / SNAPSHOT_FILENAME)


def save_snapshot(
    *,
    profile_name: str,
    home_dir: str,
    active: dict,
    archived: dict,
    fetched_at: str | None = None,
) -> dict:
    store = JsonStore(snapshot_path(home_dir))
    previous = store.load()
    current = {
        "profile": profile_name,
        "fetched_at": fetched_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "active": int(active["total"]),
        "archived": int(archived["total"]),
        "deleted": int(active["deleted"]),
        "active_pages": int(active["page_count"]),
        "archived_pages": int(archived["page_count"]),
    }
    current["total"] = current["active"] + current["archived"] + current["deleted"]
    if previous.get("fetched_at"):
        current["previous_fetched_at"] = previous.get("fetched_at")
        current["delta"] = {
            key: current[key] - int(previous.get(key, 0) or 0)
            for key in ("active", "archived", "deleted", "total")
        }
    else:
        current["previous_fetched_at"] = ""
        current["delta"] = {}
    store.save(current)
    return current


def refresh(
    *,
    profile_name: str,
    home_dir: str,
    cookies_file: str,
    base_url: str = "https://hh.ru",
) -> dict:
    negotiations_url = f"{base_url.rstrip('/')}/applicant/negotiations"
    with _authenticated_client(cookies_file) as client:
        active = _fetch_page(
            client,
            negotiations_url,
            expected_filter="active",
        )
        archived = _fetch_page(
            client,
            f"{negotiations_url}?filter=archived",
            expected_filter="archived",
        )
    return save_snapshot(
        profile_name=profile_name,
        home_dir=home_dir,
        active=active,
        archived=archived,
    )
