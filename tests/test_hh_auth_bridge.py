import asyncio
import stat

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

    hh_auth_bridge.write_response(request_id, "123456", profile_name="qa")
    answer = asyncio.run(
        hh_auth_bridge.wait_for_response(
            request_id,
            timeout_s=1,
            poll_interval_s=0.01,
            profile_name="qa",
        )
    )
    assert answer == "123456"

    hh_auth_bridge.complete_request(request_id, profile_name="qa")
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


def test_hh_auth_bridge_isolates_concurrent_profiles_and_uses_private_files(tmp_path, monkeypatch):
    monkeypatch.setattr(hh_auth_bridge.config, "HH_AUTH_BRIDGE_DIR", str(tmp_path))

    alice_id = hh_auth_bridge.create_request(
        kind="code",
        profile_name="alice",
        prompt="Alice code",
        timeout_s=60,
    )
    bob_id = hh_auth_bridge.create_request(
        kind="code",
        profile_name="bob",
        prompt="Bob code",
        timeout_s=60,
    )

    assert hh_auth_bridge.peek_pending("alice")["id"] == alice_id
    assert hh_auth_bridge.peek_pending("bob")["id"] == bob_id

    hh_auth_bridge.write_response(alice_id, "1111", profile_name="alice")
    hh_auth_bridge.write_response(bob_id, "2222", profile_name="bob")

    assert asyncio.run(
        hh_auth_bridge.wait_for_response(alice_id, timeout_s=1, profile_name="alice")
    ) == "1111"
    assert asyncio.run(
        hh_auth_bridge.wait_for_response(bob_id, timeout_s=1, profile_name="bob")
    ) == "2222"

    state_files = list(tmp_path.glob("hh_auth_*.json"))
    assert len(state_files) == 4
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in state_files)
