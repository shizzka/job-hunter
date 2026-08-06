import sys
from pathlib import Path
from types import SimpleNamespace

import captcha_solver


def test_captcha_retry_markup_uses_hh_auth_callback_for_auth_stage():
    markup = captcha_solver._captcha_retry_markup("hh_auth:qa:login_submit", "abc123")

    button = markup["inline_keyboard"][0][0]
    assert button["text"] == "🔐 Повторить вход HH"
    assert button["callback_data"] == "hh_reauth:qa"


def test_captcha_retry_markup_keeps_search_callback_for_search_stage():
    markup = captcha_solver._captcha_retry_markup("apply_submit", "abc123")

    button = markup["inline_keyboard"][0][0]
    assert button["text"] == "🔁 Перезапустить поиск"
    assert button["callback_data"] == "captcha_retry:abc123"


def test_captcha_solver_escalates_to_telegram_after_two_empty_vision_attempts(tmp_path, monkeypatch):
    class FakePage:
        url = "https://hh.ru/account/captcha"

    class FakeClient:
        def __init__(self):
            self._page = FakePage()

        async def _detect_anti_bot_kind(self):
            return "captcha"

    vision_calls = []
    photo_calls = []
    cooldown_calls = []

    async def fake_refresh_screenshot(page):
        shot = tmp_path / f"captcha_{len(vision_calls)}.png"
        shot.write_bytes(b"png")
        return str(shot)

    async def fake_solve_with_vision(screenshot_path, llm_client_factory):
        vision_calls.append(screenshot_path)
        return None

    async def fake_send_photo(path, caption="", reply_markup=None):
        photo_calls.append((path, caption, reply_markup))
        return True

    async def fake_send_message_with_markup(text, reply_markup=None):
        return True

    def fake_create_request(screenshot_path, page_url="", timeout_s=300):
        return "req1"

    async def fake_wait_for_response(request_id, timeout_s=300, poll_interval_s=2.0):
        return None

    def fake_complete_request(request_id):
        return None

    def fake_record_soft_cooldown(minutes, reason):
        cooldown_calls.append((minutes, reason))

    monkeypatch.setattr(captcha_solver.config, "HH_CAPTCHA_VISION_RETRIES", 2)
    monkeypatch.setattr(captcha_solver.config, "HH_CAPTCHA_HUMAN_WINDOW_S", 60)
    monkeypatch.setattr(captcha_solver, "_refresh_screenshot", fake_refresh_screenshot)
    monkeypatch.setattr(captcha_solver, "solve_captcha_with_vision_llm", fake_solve_with_vision)
    monkeypatch.setitem(sys.modules, "notifier", SimpleNamespace(
        send_photo=fake_send_photo,
        send_message_with_markup=fake_send_message_with_markup,
    ))
    monkeypatch.setitem(sys.modules, "captcha_bridge", SimpleNamespace(
        create_request=fake_create_request,
        wait_for_response=fake_wait_for_response,
        complete_request=fake_complete_request,
    ))
    monkeypatch.setitem(sys.modules, "hh_guard", SimpleNamespace(record_soft_cooldown=fake_record_soft_cooldown))

    import asyncio

    solved = asyncio.run(captcha_solver.try_solve_captcha_interactively(
        FakeClient(),
        lambda: object(),
        stage="hh_auth:qa:login_submit",
        max_retries=3,
    ))

    assert solved is False
    assert len(vision_calls) == 2
    assert len(photo_calls) == 1
    assert "попытка 2/3" in photo_calls[0][1]
    button = photo_calls[0][2]["inline_keyboard"][0][0]
    assert button["callback_data"] == "hh_reauth:qa"
    assert cooldown_calls == [(15, "captcha TG timeout")]
