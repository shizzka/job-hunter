"""R2 contract: the orchestrator asks hh.integrity one question instead of reading
private session state. These cases pin the answers the orchestrator relies on."""
import asyncio
from types import SimpleNamespace

import apply_orchestrator
from hh import integrity
from hh.browser import _stop_browser_owned, is_completed_owned_stop


def test_no_client_or_no_attempt_leaves_the_result():
    assert integrity.session_permits_result(None, "https://hh.ru/vacancy/1") is True
    client = SimpleNamespace(_hh_recovery_uncertain=False, _page=None, _context=None, _external_attempt=None)
    assert integrity.session_permits_result(client, "https://hh.ru/vacancy/1") is True


def test_sticky_uncertainty_or_unknown_action_answers_false():
    client = SimpleNamespace(_hh_recovery_uncertain=True)
    assert integrity.session_permits_result(client, "u") is False
    client = SimpleNamespace(_hh_recovery_uncertain=False, _context=None, _external_attempt=None)
    client._page = SimpleNamespace(_hh_action_monitor=SimpleNamespace(session=client, unknown=True))
    assert integrity.session_permits_result(client, "u") is False


def test_unreadable_state_answers_false():
    class Broken:
        @property
        def _hh_recovery_uncertain(self):
            raise RuntimeError("lost")
    assert integrity.session_permits_result(Broken(), "u") is False


def test_orchestrator_turns_false_into_uncertain(monkeypatch):
    monkeypatch.setattr(integrity, "session_permits_result", lambda *a, **k: False)
    result = apply_orchestrator._apply_result_with_current_uncertainty(
        {"source": "hh", "url": "u"}, object(), {"ok": True, "message": "sent"})
    assert result["uncertain"] is True and result["ok"] is False
    monkeypatch.setattr(integrity, "session_permits_result", lambda *a, **k: True)
    result = apply_orchestrator._apply_result_with_current_uncertainty(
        {"source": "hh", "url": "u"}, object(), {"ok": True, "message": "sent"})
    assert result == {"ok": True, "message": "sent"}


def test_completed_owned_stop_is_only_the_finished_shutdown_task():
    async def other():
        return None

    async def run():
        session = SimpleNamespace(_context=None, _browser=None, _pw=None, _page=None, _cookie_binding=None)
        stop = asyncio.create_task(_stop_browser_owned(session, (None, None, None, None, None),
                                                       save_cookies=None, terminate=None))
        unrelated = asyncio.create_task(other())
        assert not is_completed_owned_stop(stop)  # not finished yet
        await asyncio.gather(stop, unrelated, return_exceptions=True)
        return stop, unrelated

    stop, unrelated = asyncio.run(run())
    assert is_completed_owned_stop(unrelated) is False
    assert stop.exception() is None and is_completed_owned_stop(stop) is True
    assert is_completed_owned_stop(object()) is False
