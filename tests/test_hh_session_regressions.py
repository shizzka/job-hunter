"""Black-box regressions also runnable unchanged on the pre-fix source tree."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from hh import browser
import hh_response_counter as counter
from state_store.json_store import atomic_write_json
from tests.test_hh_browser import (FakeLifecycleContext, FakeLifecycleBrowser, FakeChromium,
                             FakePlaywright, FakePlaywrightStarter)


def cookies(value):
    return [{"name": "hhtoken", "value": value, "domain": ".hh.ru", "path": "/"}]


@pytest.fixture
def session(tmp_path, monkeypatch):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    atomic_write_json(a, cookies("profile-a"))
    atomic_write_json(b, cookies("profile-b"))
    monkeypatch.setattr(browser.config, "HH_COOKIES_FILE", str(a))
    monkeypatch.setattr(browser.config, "HH_STATE_DIR", str(tmp_path / "state"))
    context = FakeLifecycleContext(cookies("rotated-a"))
    pw = FakePlaywright(FakeChromium(FakeLifecycleBrowser(context)))
    current = SimpleNamespace(_context=None, _browser=None, _pw=None, _page=None)
    async def start():
        await browser.start_browser(current,
            playwright_factory=lambda: FakePlaywrightStarter(pw), stealth_available=False,
            proxy_env_builder=lambda _: {})
    return current, context, pw, start, a, b


def test_start_cannot_load_other_profiles_cookies_after_await(session, monkeypatch):
    current, context, pw, start, a, b = session
    async def first_await(*args):
        monkeypatch.setattr(browser.config, "HH_COOKIES_FILE", str(b))
        return pw
    monkeypatch.setattr(FakePlaywrightStarter, "start", first_await)
    asyncio.run(start())
    assert context.loaded_cookies == cookies("profile-a")


def test_cookie_capture_cannot_save_into_other_profile(session, monkeypatch):
    current, context, pw, start, a, b = session
    asyncio.run(start())
    async def capture():
        monkeypatch.setattr(browser.config, "HH_COOKIES_FILE", str(b))
        return cookies("rotated-a")
    context.cookies = capture
    asyncio.run(browser.save_session(current))
    assert json.loads(b.read_text()) == cookies("profile-b")
    assert json.loads(a.read_text()) == cookies("rotated-a")


def test_old_browser_cannot_erase_newly_persisted_login(session):
    current, context, pw, start, a, b = session
    asyncio.run(start())
    browser._save_cookies(cookies("new-login"))
    asyncio.run(browser.stop_browser(current))
    assert json.loads(a.read_text()) == cookies("new-login")


def save(home, active, stamp):
    return counter.save_snapshot(profile_name="synthetic", home_dir=str(home),
        active={"total": active, "deleted": 1, "page_count": 1},
        archived={"total": 2, "page_count": 1}, fetched_at=stamp)


def test_older_counter_observation_cannot_replace_newer(tmp_path):
    newer = save(tmp_path, 20, "2026-10-03T11:00:00+03:00")
    assert save(tmp_path, 10, "2026-10-03T10:00:00+03:00") == newer


def test_counter_refresh_preserves_unrelated_metadata(tmp_path):
    first = save(tmp_path, 10, "2026-10-03T10:00:00+03:00")
    atomic_write_json(tmp_path / counter.SNAPSHOT_FILENAME, {**first, "custom": "keep"})
    assert save(tmp_path, 20, "2026-10-03T11:00:00+03:00")["custom"] == "keep"


def test_counter_corruption_is_not_replaced(tmp_path):
    path = tmp_path / counter.SNAPSHOT_FILENAME
    path.write_bytes(b"{")
    with pytest.raises(RuntimeError):
        save(tmp_path, 10, "2026-10-03T10:00:00+03:00")
    assert path.read_bytes() == b"{"
