"""Baseline-compatible fake-browser regressions; no new storage API required."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import habr_career_client as habr
import geekjob_client as geekjob
import superjob_client as superjob
from state_store.json_store import atomic_write_json
from tests.test_hh_browser import (FakeLifecycleContext, FakeLifecycleBrowser,
                                  FakeChromium, FakePlaywright, FakePlaywrightStarter)

SOURCES = [(habr, habr.HabrCareerClient, "HABR_COOKIES_FILE"),
           (geekjob, geekjob.GeekJobClient, "GEEKJOB_COOKIES_FILE"),
           (superjob, superjob.SuperJobClient, "SUPERJOB_COOKIES_FILE")]


def cookies(value):
    return [{"name": "session", "value": value, "domain": ".geekjob.ru", "path": "/"}]


@pytest.fixture(params=SOURCES, ids=["habr", "geekjob", "superjob"])
def native_case(request, tmp_path, monkeypatch):
    module, client_type, setting = request.param
    original, other = tmp_path / "a" / "cookies.json", tmp_path / "b" / "cookies.json"
    monkeypatch.setattr(module.config, setting, str(original))
    monkeypatch.setattr(module.config, "HH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(module.config, "SUPERJOB_AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setattr(module.config, "BROWSER_PROXY", "")
    monkeypatch.setattr(module.config, "HEADLESS", True)
    monkeypatch.setattr(module.config, "SLOW_MO", 0)
    for key in ["HH_PROXY", "HABR_PROXY", "GEEKJOB_PROXY"]:
        monkeypatch.delenv(key, raising=False)
    atomic_write_json(original, cookies("original"))
    atomic_write_json(other, cookies("other"))
    context = FakeLifecycleContext(cookies("rotated"))
    browser = FakeLifecycleBrowser(context)
    pw = FakePlaywright(FakeChromium(browser))
    starter = FakePlaywrightStarter(pw)
    monkeypatch.setattr(module, "async_playwright", lambda: starter)
    monkeypatch.setattr(module.proxy_utils, "browser_launch_env", lambda _: {})
    client = client_type()
    return SimpleNamespace(module=module, client=client, setting=setting, original=original,
        other=other, context=context, browser=browser, pw=pw, starter=starter,
        read=lambda path: json.loads(path.read_text()))


def test_start_loads_original_profile_before_first_await(native_case, monkeypatch):
    case = native_case
    async def begin():
        monkeypatch.setattr(case.module.config, case.setting, str(case.other))
        monkeypatch.setattr(case.module.config, "HEADLESS", False)
        return case.pw
    monkeypatch.setattr(case.starter, "start", begin)
    asyncio.run(case.client.start_browser())
    assert case.context.loaded_cookies == cookies("original")
    assert case.pw.chromium.launch_options["headless"] is True


@pytest.mark.parametrize("shutdown", [False, True])
def test_save_after_profile_switch_keeps_original_destination(native_case, monkeypatch, shutdown):
    case = native_case
    asyncio.run(case.client.start_browser())
    async def capture():
        monkeypatch.setattr(case.module.config, case.setting, str(case.other))
        return cookies("rotated")
    case.context.cookies = capture
    asyncio.run(case.client.stop_browser() if shutdown else case.client.save_session())
    assert case.read(case.original) == cookies("rotated")
    assert case.read(case.other) == cookies("other")


def test_stale_shutdown_cannot_replace_new_login(native_case):
    case = native_case
    asyncio.run(case.client.start_browser())
    case.module._save_cookies(cookies("new-login"))
    asyncio.run(case.client.stop_browser())
    assert case.read(case.original) == cookies("new-login")
    assert case.browser.closed and case.pw.stopped


def test_full_stop_closes_browser(native_case):
    case = native_case
    asyncio.run(case.client.start_browser())
    asyncio.run(case.client.stop())
    assert case.browser.closed and case.pw.stopped
    assert case.client._context is None


def test_corrupt_cache_cannot_be_silently_replaced(native_case):
    case = native_case
    case.original.write_bytes(b"corrupt-synthetic-json")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            case.module._save_cookies(cookies("replacement"))
        assert case.original.read_bytes() == b"corrupt-synthetic-json"


def test_geekjob_http_cookie_read_uses_constructed_profile(tmp_path, monkeypatch):
    first, other = tmp_path / "first.json", tmp_path / "other.json"
    atomic_write_json(first, cookies("original"))
    atomic_write_json(other, cookies("other"))
    monkeypatch.setattr(geekjob.config, "GEEKJOB_COOKIES_FILE", str(first))
    monkeypatch.setattr(geekjob.config, "HH_STATE_DIR", str(tmp_path / "state"))
    client = geekjob.GeekJobClient()
    monkeypatch.setattr(geekjob.config, "GEEKJOB_COOKIES_FILE", str(other))
    captured = []
    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def text(self): return "{}"
    class Session:
        def request(self, *args, **kwargs):
            captured.append(kwargs["headers"])
            return Response()
    client._session = Session()
    asyncio.run(client._request_json_once("GET", "/synthetic"))
    assert captured[0]["Cookie"] == "session=original"
