import asyncio

import captcha_bridge


def test_captcha_requests_and_answers_are_isolated_by_profile(tmp_path, monkeypatch):
    def state_dir(profile_name=None):
        path = tmp_path / (profile_name or "default")
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    monkeypatch.setattr(captcha_bridge, "_state_dir", state_dir)
    shot = tmp_path / "captcha.png"
    shot.write_bytes(b"png")

    request_id = captcha_bridge.create_request(
        str(shot),
        profile_name="client_marina",
        timeout_s=60,
    )

    pending = captcha_bridge.peek_pending("client_marina")
    assert pending["id"] == request_id
    assert pending["profile_name"] == "client_marina"
    assert captcha_bridge.peek_pending("qa") is None

    captcha_bridge.write_response(request_id, "текст", profile_name="client_marina")
    answer = asyncio.run(captcha_bridge.wait_for_response(request_id, profile_name="client_marina"))
    assert answer == "текст"
    assert captcha_bridge.peek_pending("client_marina") is None
