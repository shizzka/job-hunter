"""A pre-submit browser guard must retain its origin without clearing uncertainty."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hh import apply
from hh import recovery, verification
from hh.ui import HHUnexpectedUI
from hh_client import HHClient
from state_store.native_apply import NativeApplyRepository


@pytest.mark.parametrize('action_started,post_count,scope', [
    (False, 0, 'browser_preflight'), (True, 0, 'native_action'),
    (False, 1, 'native_action'), (False, None, 'unproven'),
])
def test_guard_preserves_actual_stage_and_native_action_evidence(tmp_path, monkeypatch, action_started, post_count, scope):
    client = HHClient()
    client._cookie_paths = SimpleNamespace(cookies_file=tmp_path / 'cookies.json')
    client._page = SimpleNamespace()
    client._context = SimpleNamespace(_hh_action_watch=SimpleNamespace(unknown=False, application_post_sequence=post_count))
    url = 'https://hh.ru/vacancy/123'

    async def guarded(*args, **kwargs):
        attempt = client._external_attempt
        if action_started:
            attempt.begin()
            attempt.command_started = True
        attempt.uncertain = True
        error = HHUnexpectedUI('resume_preflight', 'synthetic_guard')
        error.hh_uncertain = True
        error.hh_stop_reason = 'possible_external_action'
        raise error
    monkeypatch.setattr(apply, '_apply_to_vacancy', guarded)
    monkeypatch.setattr(verification, 'reconcile_post_submit', AsyncMock())
    monkeypatch.setattr(recovery, 'recover_unexpected_ui', AsyncMock(return_value=SimpleNamespace(recovered=False)))
    result = asyncio.run(apply.apply_to_vacancy(client, url, preferred_resume_id='synthetic-exact',
        absolute_hh_url=lambda value: value, anti_bot_message=lambda *args: '', logger=SimpleNamespace()))
    assert result['ok'] is False and result['uncertain'] is True
    assert result['hh_failure_stage'] == 'resume_preflight'
    assert result['hh_uncertainty_scope'] == scope
    evidence = result['hh_guard_failure']
    assert evidence['reason'] == 'possible_external_action'
    assert evidence['native_acting'] is action_started
    assert evidence['command_started'] is action_started
    assert evidence['application_posts'] == post_count
    repository = NativeApplyRepository(client._cookie_paths.cookies_file, 'hh')
    assert repository.get(url)['status'] == 'uncertain'
    assert not repository.claim(url, '', '')


def test_dispatch_trace_uses_failing_guard_stage_instead_of_last_success(tmp_path, monkeypatch):
    import apply_orchestrator
    import config
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', False)
    monkeypatch.setattr(apply_orchestrator.company_blacklist, 'is_blocked', lambda value: False)
    trace = SimpleNamespace(last_stage='EXISTING_RESPONSE_CHECK', event=lambda *args, **kwargs: None,
                            finish=lambda **kwargs: finished.append(kwargs))
    finished = []
    lower = {'ok': False, 'uncertain': True, 'hh_failure_stage': 'resume_preflight',
             'hh_guard_failure': {'reason': 'possible_external_action'},
             'hh_uncertainty_scope': 'browser_preflight'}
    monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', AsyncMock(return_value=lower))
    result = asyncio.run(apply_orchestrator.dispatch_apply(
        {'id': '123', 'url': 'https://hh.ru/vacancy/123', 'source': 'hh'}, 'Synthetic',
        preferred_resume_id='synthetic-exact', trace=trace))
    assert result['uncertain'] is True
    assert result['hh_uncertainty_scope'] == 'browser_preflight'
    assert finished[-1]['failure_stage'] == 'resume_preflight'
