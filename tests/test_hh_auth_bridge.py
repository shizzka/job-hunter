import asyncio

import hh_auth_bridge


def test_hh_auth_bridge_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(hh_auth_bridge.config, "HH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(hh_auth_bridge.config, "HH_AUTH_BRIDGE_DIR", str(tmp_path))

    request_id = hh_auth_bridge.create_request(
        kind="code",
        profile_name="qa",
        prompt="Введите код",
        timeout_s=60,
    )

    pending = hh_auth_bridge.peek_pending("qa")
    assert pending["id"] == request_id
    assert pending["kind"] == "code"

    hh_auth_bridge.write_response(request_id, "123456")
    answer = asyncio.run(hh_auth_bridge.wait_for_response(request_id, timeout_s=1, poll_interval_s=0.01))
    assert answer == "123456"

    hh_auth_bridge.complete_request(request_id)
    assert hh_auth_bridge.peek_pending("qa") is None


def test_hh_auth_bridge_keeps_shared_path_when_profile_state_changes(tmp_path, monkeypatch):
    shared = tmp_path / "shared-state"
    monkeypatch.setattr(hh_auth_bridge.config, "HH_AUTH_BRIDGE_DIR", str(shared))
    monkeypatch.setattr(hh_auth_bridge.config, "HH_STATE_DIR", str(tmp_path / "profiles" / "client_42" / "state"))

    request_id = hh_auth_bridge.create_request(
        kind="code",
        profile_name="client_42",
        prompt="Введите код",
        timeout_s=60,
    )

    # Telegram bot runs under another active profile with another HH_STATE_DIR.
    monkeypatch.setattr(hh_auth_bridge.config, "HH_STATE_DIR", str(tmp_path / "default" / "state"))
    assert hh_auth_bridge.peek_pending("client_42")["id"] == request_id
