"""Exact HH response counters fetched from the authenticated negotiations pages."""

from __future__ import annotations

import html
import os
import re
from datetime import datetime
from pathlib import Path

import httpx

from state_store.hh_response_counter import HHCounterRepository, observation_time, valid_snapshot
from state_store.hh_cookies import HHCookieRepository
from state_store.json_store import file_lock


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
        cookies, _ = HHCookieRepository(cookies_file).snapshot()
    except Exception as exc:
        raise HHResponseCounterError(f"Не удалось прочитать сессию HH: {type(exc).__name__}") from exc
    if cookies is None:
        raise HHResponseCounterError(
            "Сессия HH не найдена. Сначала выполните «Вход HH»."
        )
    if not isinstance(cookies, list) or not cookies:
        raise HHResponseCounterError(
            "Сессия HH пуста. Сначала выполните «Вход HH»."
        )
    return [item for item in cookies if isinstance(item, dict)]


def _authenticated_client(cookies_file: str, *, cookies: list[dict] | None = None) -> httpx.Client:
    cookies = _load_playwright_cookies(cookies_file) if cookies is None else cookies
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
    for cookie in cookies:
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
    refresh_sequence: int | None = None,
) -> dict:
    if not isinstance(profile_name, str) or not profile_name:
        raise ValueError("HH counter profile is required")
    repository = HHCounterRepository(snapshot_path(home_dir))
    current = {
        "profile": profile_name,
        "fetched_at": fetched_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "active": active["total"],
        "archived": archived["total"],
        "deleted": active["deleted"],
        "active_pages": active["page_count"],
        "archived_pages": archived["page_count"],
    }
    current["total"] = current["active"] + current["archived"] + current["deleted"]
    observation_time(current["fetched_at"])
    if not valid_snapshot(current):
        raise ValueError("Invalid HH counter observation")
    sequence = repository.begin(profile_name) if refresh_sequence is None else refresh_sequence
    return repository.commit(current, sequence, enforce_timestamp=refresh_sequence is None)


def refresh(
    *,
    profile_name: str,
    home_dir: str,
    cookies_file: str,
    base_url: str = "https://hh.ru",
) -> dict:
    home_dir = os.path.abspath(os.fspath(home_dir))
    cookies_file = os.path.abspath(os.fspath(cookies_file))
    repository = HHCounterRepository(snapshot_path(home_dir))
    cookie_repository = HHCookieRepository(cookies_file)
    cookies, cookie_revision = cookie_repository.snapshot()
    if not cookies:
        raise HHResponseCounterError("Сессия HH пуста или не найдена. Сначала выполните «Вход HH».")
    # Ticket before requests: later completion cannot roll back a newer refresh.
    sequence = repository.begin(profile_name)
    fetched_at = datetime.now().astimezone().isoformat(timespec="microseconds")
    negotiations_url = f"{base_url.rstrip('/')}/applicant/negotiations"
    with _authenticated_client(cookies_file, cookies=cookies) as client:
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
    # Lock order: cookies -> counter sidecar, synchronous disk work only.
    with file_lock(cookie_repository.path):
        if cookie_repository._snapshot_unlocked()[1] != cookie_revision:
            raise HHResponseCounterError("Сессия HH изменилась во время обновления; повторите проверку.")
        return save_snapshot(profile_name=profile_name, home_dir=home_dir,
            active=active, archived=archived, fetched_at=fetched_at, refresh_sequence=sequence)
