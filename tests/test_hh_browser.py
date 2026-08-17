import asyncio
import json
from types import SimpleNamespace

import hh_client
from hh import browser


def test_cookie_helpers_roundtrip_configured_state_file(tmp_path, monkeypatch):
    cookies_file = tmp_path / "browser" / "cookies.json"
    state_dir = tmp_path / "state"
    monkeypatch.setattr(browser.config, "HH_COOKIES_FILE", str(cookies_file))
    monkeypatch.setattr(browser.config, "HH_STATE_DIR", str(state_dir))
    cookies = [{"name": "hhtoken", "value": "токен", "domain": ".hh.ru"}]

    browser._save_cookies(cookies)

    assert browser._load_cookies() == cookies
    assert state_dir.is_dir()
    assert json.loads(cookies_file.read_text()) == cookies


def test_load_cookies_returns_none_when_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(browser.config, "HH_COOKIES_FILE", str(tmp_path / "missing.json"))

    assert browser._load_cookies() is None


def test_legacy_module_reexports_cookie_helpers():
    assert hh_client._ensure_dirs is browser._ensure_dirs
    assert hh_client._load_cookies is browser._load_cookies
    assert hh_client._save_cookies is browser._save_cookies


class FakeLifecycleContext:
    def __init__(self, cookies):
        self.loaded_cookies = []
        self.saved_cookies = cookies
        self.page = object()

    async def add_cookies(self, cookies):
        self.loaded_cookies.extend(cookies)

    async def new_page(self):
        return self.page

    async def cookies(self):
        return self.saved_cookies


class FakeLifecycleBrowser:
    def __init__(self, context):
        self.context = context
        self.context_options = {}
        self.closed = False

    async def new_context(self, **kwargs):
        self.context_options = kwargs
        return self.context

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser_instance):
        self.browser_instance = browser_instance
        self.launch_options = {}

    async def launch(self, **kwargs):
        self.launch_options = kwargs
        return self.browser_instance


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakePlaywrightStarter:
    def __init__(self, playwright):
        self.playwright = playwright

    async def start(self):
        return self.playwright


def test_start_browser_builds_context_loads_cookies_and_applies_stealth(monkeypatch):
    cookies = [{"name": "hhtoken", "value": "secret"}]
    context = FakeLifecycleContext(cookies)
    browser_instance = FakeLifecycleBrowser(context)
    chromium = FakeChromium(browser_instance)
    playwright = FakePlaywright(chromium)
    starter = FakePlaywrightStarter(playwright)
    stealth_calls = []
    ensure_calls = []
    proxy_url = "http://127.0.0.1:7897"
    monkeypatch.setenv("HH_PROXY", proxy_url)
    settings = SimpleNamespace(HEADLESS=True, SLOW_MO=75, BROWSER_PROXY="")
    session = SimpleNamespace(_pw=None, _browser=None, _context=None, _page=None)

    class FakeStealth:
        async def apply_stealth_async(self, target_context):
            stealth_calls.append(target_context)

    asyncio.run(
        browser.start_browser(
            session,
            headless=False,
            settings=settings,
            playwright_factory=lambda: starter,
            stealth_available=True,
            stealth_factory=FakeStealth,
            proxy_env_builder=lambda value: {"BROWSER_PROXY": value},
            ensure_dirs=lambda: ensure_calls.append(True),
            load_cookies=lambda: cookies,
        )
    )

    assert ensure_calls == [True]
    assert session._pw is playwright
    assert session._browser is browser_instance
    assert session._context is context
    assert session._page is context.page
    assert chromium.launch_options == {
        "headless": False,
        "slow_mo": 75,
        "proxy": {"server": proxy_url},
        "env": {"BROWSER_PROXY": proxy_url},
    }
    assert browser_instance.context_options["viewport"] == {"width": 1280, "height": 900}
    assert browser_instance.context_options["locale"] == "ru-RU"
    assert "Chrome/131.0.0.0" in browser_instance.context_options["user_agent"]
    assert context.loaded_cookies == cookies
    assert stealth_calls == [context]


def test_stop_browser_saves_cookies_and_closes_resources():
    cookies = [{"name": "hhtoken", "value": "secret"}]
    context = FakeLifecycleContext(cookies)
    browser_instance = FakeLifecycleBrowser(context)
    playwright = FakePlaywright(FakeChromium(browser_instance))
    session = SimpleNamespace(
        _context=context,
        _browser=browser_instance,
        _pw=playwright,
    )
    saved = []

    asyncio.run(browser.stop_browser(session, save_cookies=saved.append))

    assert saved == [cookies]
    assert browser_instance.closed is True
    assert playwright.stopped is True


def test_hh_client_lifecycle_wrappers_forward_patchable_dependencies(monkeypatch):
    start_call = {}
    stop_call = {}

    async def fake_start(session, headless, **kwargs):
        start_call["session"] = session
        start_call["headless"] = headless
        start_call.update(kwargs)

    async def fake_stop(session, **kwargs):
        stop_call["session"] = session
        stop_call.update(kwargs)

    monkeypatch.setattr(hh_client, "_start_browser", fake_start)
    monkeypatch.setattr(hh_client, "_stop_browser", fake_stop)
    client = hh_client.HHClient()

    asyncio.run(client.start(headless=False))
    asyncio.run(client.stop())

    assert start_call["session"] is client
    assert start_call["headless"] is False
    assert start_call["settings"] is hh_client.config
    assert start_call["playwright_factory"] is hh_client.async_playwright
    assert start_call["stealth_available"] is hh_client._STEALTH_AVAILABLE
    assert start_call["stealth_factory"] is hh_client.Stealth
    assert start_call["proxy_env_builder"] is hh_client.proxy_utils.browser_launch_env
    assert start_call["ensure_dirs"] is hh_client._ensure_dirs
    assert start_call["load_cookies"] is hh_client._load_cookies
    assert start_call["logger"] is hh_client.log
    assert stop_call == {
        "session": client,
        "save_cookies": hh_client._save_cookies,
    }
