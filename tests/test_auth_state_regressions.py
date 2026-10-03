"""Black-box regressions runnable on the previous tree; synthetic data only."""
import asyncio
import json
from pathlib import Path

import pytest

import superjob_client as sj
import hh_auth_bridge as hh
import captcha_bridge as captcha
from state_store.json_store import atomic_write_json


def auth(token):
    return {"access_token": token, "refresh_token": "refresh-" + token,
            "expires_at": 1, "user": {"id": 7}, "resume_id": 9, "unknown": "retain"}


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    first, other = tmp_path / "a.json", tmp_path / "b.json"
    for path, token in [(first, "original"), (other, "other")]:
        atomic_write_json(path, auth(token))
    monkeypatch.setattr(sj.config, "SUPERJOB_AUTH_FILE", str(first))
    monkeypatch.setattr(sj.config, "SUPERJOB_COOKIES_FILE", str(tmp_path / "cookies.json"))
    monkeypatch.setattr(sj.config, "HH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(sj.config, "SUPERJOB_API_KEY", "synthetic-app-key")
    monkeypatch.setattr(sj.config, "SUPERJOB_CLIENT_ID", 7)
    return sj.SuperJobClient(), first, other


def test_password_login_keeps_original_destination(credentials, monkeypatch):
    client, first, other = credentials
    async def request(*args, **kwargs):
        monkeypatch.setattr(sj.config, "SUPERJOB_AUTH_FILE", str(other))
        return {"access_token": "fresh", "expires_in": 900}
    monkeypatch.setattr(client, "_request", request)
    asyncio.run(client.password_login("synthetic", "synthetic"))
    assert json.loads(first.read_text())["access_token"] == "fresh"
    assert json.loads(other.read_text()) == auth("other")


def test_refresh_cannot_replace_new_login(credentials, monkeypatch):
    client, first, _ = credentials
    async def request(*args, **kwargs):
        sj._save_auth_file(auth("new-login"))
        return {"access_token": "stale", "expires_in": 900}
    monkeypatch.setattr(client, "_request", request)
    assert asyncio.run(client.refresh_access_token()) is False
    assert json.loads(first.read_text()) == auth("new-login")


def test_competing_refreshes_do_not_send_two_rotation_requests(credentials, monkeypatch):
    client, first, _ = credentials
    second = sj.SuperJobClient()
    calls = []
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def request(*args, **kwargs):
            calls.append(True)
            entered.set()
            await release.wait()
            return {"access_token": "fresh", "expires_in": 900}
        monkeypatch.setattr(client, "_request", request)
        monkeypatch.setattr(second, "_request", request)
        one = asyncio.create_task(client.refresh_access_token())
        await entered.wait()
        two = asyncio.create_task(second.refresh_access_token())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(one, two)
    asyncio.run(scenario())
    assert len(calls) == 1


def test_cached_auth_cannot_adopt_another_account(credentials):
    client, first, _ = credentials
    assert "original" in client._auth_header()
    sj._save_auth_file(auth("new-login"))
    with pytest.raises(RuntimeError): client._auth_header()


@pytest.mark.parametrize("content", [b'not-json', b'\xff', b'[]', b'{"access_token":42}'])
def test_corrupt_auth_cannot_be_replaced(credentials, content):
    _, first, _ = credentials
    first.write_bytes(content)
    with pytest.raises(RuntimeError): sj._save_auth_file(auth("replacement"))
    assert first.read_bytes() == content


@pytest.fixture(params=[hh, captcha], ids=["hh", "captcha"])
def mailbox(request, tmp_path, monkeypatch):
    module = request.param
    monkeypatch.setattr(module, "_state_dir", lambda *args: str(tmp_path))
    create = (lambda: module.create_request(kind="code", profile_name="qa", prompt="synthetic", timeout_s=60)
              if module is hh else module.create_request("synthetic.png", profile_name="qa", timeout_s=60))
    return module, create, tmp_path


def test_late_reply_cannot_replace_new_prompt_response(mailbox):
    module, create, _ = mailbox
    old, new = create(), create()
    module.write_response(new, "new-answer", profile_name="qa")
    with pytest.raises((ValueError, RuntimeError)):
        module.write_response(old, "old-answer", profile_name="qa")
    assert asyncio.run(module.wait_for_response(new, timeout_s=1, profile_name="qa")) == "new-answer"


def test_first_answer_cannot_be_overwritten(mailbox):
    module, create, _ = mailbox
    request_id = create()
    module.write_response(request_id, "first", profile_name="qa")
    with pytest.raises((ValueError, RuntimeError)):
        module.write_response(request_id, "late", profile_name="qa")
    assert asyncio.run(module.wait_for_response(request_id, timeout_s=1, profile_name="qa")) == "first"


def test_corrupt_pending_is_not_deleted_by_cleanup(mailbox):
    module, create, _ = mailbox
    request_id = create()
    pending = Path(module._pending_path("qa"))
    pending.write_bytes(b'corrupt')
    try: module.complete_request(request_id, profile_name="qa")
    except RuntimeError: pass
    assert pending.read_bytes() == b'corrupt'


def test_wait_keeps_preawait_mailbox_destination(mailbox, tmp_path, monkeypatch):
    module, create, directory = mailbox
    request_id = create()
    original_response = Path(module._response_path("qa"))
    other = tmp_path / "other"
    other.mkdir()
    calls = []
    async def sleep(_):
        calls.append(True)
        if len(calls) > 2: raise AssertionError("Wait followed another mailbox directory")
        monkeypatch.setattr(module, "_state_dir", lambda *args: str(other))
        atomic_write_json(original_response, {"id": request_id, "answer": "original", "received_at": 1})
    monkeypatch.setattr(asyncio, "sleep", sleep)
    assert asyncio.run(module.wait_for_response(request_id, timeout_s=1, profile_name="qa")) == "original"
