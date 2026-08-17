import json

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
