"""Independent shutdown ownership and sticky outcome regressions; no network."""
import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import config
import seen
from hh.browser import CookieBinding, stop_browser
from state_store.hh_cookies import HHCookieRepository
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from state_store.private_journal import read_json_records
from tests.test_observability import configure_synthetic_search


def session_for_stop(tmp_path, cookies):
    context = SimpleNamespace(cookies=cookies)
    browser, page = SimpleNamespace(), SimpleNamespace()
    binding = CookieBinding(HHCookieRepository(tmp_path / 'cookies.json'), None, False, context)
    session = SimpleNamespace(_context=context, _browser=browser, _page=page,
                              _pw=SimpleNamespace(stop=AsyncMock()), _cookie_binding=binding,
                              _cookie_write_nonce=object())
    return session


def test_concurrent_stop_waits_for_confirmed_termination(tmp_path):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        session = session_for_stop(tmp_path, AsyncMock(return_value=[]))

        async def terminate(browser):
            entered.set()
            await release.wait()
            return True

        first = asyncio.create_task(stop_browser(session, terminate=terminate))
        await entered.wait()
        second = asyncio.create_task(stop_browser(session, terminate=terminate))
        await asyncio.sleep(0)
        returned_before_death = second.done()
        release.set()
        await asyncio.gather(first, second)
        assert not returned_before_death, (
            'Every stop caller must await confirmed process death'
        )

    asyncio.run(scenario())


def test_cancelled_stop_waits_for_confirmed_termination_before_propagating(tmp_path):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        session = session_for_stop(tmp_path, AsyncMock(return_value=[]))

        async def terminate(browser):
            entered.set()
            await release.wait()
            return True

        caller = asyncio.create_task(stop_browser(session, terminate=terminate))
        await entered.wait()
        caller.cancel()
        await asyncio.sleep(0)
        returned_before_death = caller.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        # Finish the detached operation even on the old failing implementation,
        # so test cleanup itself cannot close a live browser/driver.
        await session._hh_shutdown_operation[2]
        assert not returned_before_death, (
            'Cancellation may propagate only after owned process death is confirmed'
        )

    asyncio.run(scenario())


def test_cancelled_termination_waits_for_owned_death_before_propagating(monkeypatch):
    import hh.termination as termination

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        browser = SimpleNamespace()

        async def crash_and_confirm(owned):
            assert owned is browser
            entered.set()
            await release.wait()
            return True

        monkeypatch.setattr(termination, '_crash_and_confirm', crash_and_confirm)
        caller = asyncio.create_task(termination.terminate_browser(browser))
        await entered.wait()
        caller.cancel()
        await asyncio.sleep(0)
        returned_before_death = caller.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await browser._jh_termination_operation
        assert not returned_before_death

    asyncio.run(scenario())


def test_native_begin_cannot_dispatch_while_shutdown_cookie_capture_is_pending(tmp_path):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def cookies():
            entered.set()
            await release.wait()
            return []

        session = session_for_stop(tmp_path, cookies)
        shutdown = asyncio.create_task(stop_browser(session, terminate=AsyncMock(return_value=True)))
        await entered.wait()
        repository = NativeApplyRepository(tmp_path / 'attempt-cookies.json', 'hh')
        commands = []

        async def operation():
            session._external_attempt.begin()
            commands.append('would dispatch an application')
            return {'ok': True}

        try:
            result = await run_native_attempt(session, repository, 'https://hh.ru/vacancy/4', operation)
        finally:
            release.set()
            await shutdown
        assert commands == []
        assert not result['ok'] and result['uncertain']

    asyncio.run(scenario())


@pytest.mark.parametrize('termination', ['false', 'cancelled'])
@pytest.mark.parametrize('outcome', ['uncertain', 'ui_guard'])
def test_manual_outcome_is_sticky_before_failed_or_cancelled_termination(tmp_path, monkeypatch, termination, outcome):
    def deny_network(*args, **kwargs):
        raise AssertionError('Independent shutdown regression must remain offline')

    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny_network)
    for key, value in {
        'JOB_HUNTER_HOME': str(tmp_path), 'SEEN_VACANCIES_FILE': str(tmp_path / 'seen.json'),
        'RUN_HISTORY_FILE': str(tmp_path / 'runs.jsonl'), 'RUNTIME_STATUS_FILE': str(tmp_path / 'runtime.json'),
        'ANALYTICS_EVENTS_FILE': str(tmp_path / 'events.jsonl'),
        'ANALYTICS_STATE_FILE': str(tmp_path / 'analytics.json'), 'HH_STATE_DIR': str(tmp_path / 'hh'),
        'HH_GUARD_STATE_FILE': str(tmp_path / 'guard.json'),
    }.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    configure_synthetic_search(monkeypatch, tmp_path)
    monkeypatch.setattr(config, 'HABR_ENABLED', True)
    hh = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), _page=None,
                         is_logged_in=AsyncMock(return_value=True),
                         get_negotiation_statuses=AsyncMock(return_value=[]))
    habr = SimpleNamespace(stop=AsyncMock(), stop_browser=AsyncMock(), _page=None,
                           is_logged_in=AsyncMock(return_value=True))
    expected_seen = 'apply_uncertain' if outcome == 'uncertain' else 'manual_hh_guard_stop'
    expected_decision = 'apply_uncertain' if outcome == 'uncertain' else 'guard_stop'

    async def terminate():
        # This is checked inside the first external await, not just after return.
        assert seen._load()['4']['action'] == expected_seen
        decisions = [event['decision'] for event in read_json_records(config.ANALYTICS_EVENTS_FILE)
                     if event.get('event') == 'decision']
        assert decisions == [expected_decision]
        if termination == 'cancelled':
            raise asyncio.CancelledError()
        return False

    hh.hard_stop_browser = terminate
    monkeypatch.setattr(agent, 'HHClient', lambda: hh)
    monkeypatch.setattr(agent, 'HabrCareerClient', lambda: habr)
    vacancies = [{'id': str(vid), 'source': source, 'title': f'QA {vid}',
                  'company': 'Synthetic', 'url': f'https://hh.ru/vacancy/{vid}'}
                 for vid, source in [(4, 'hh'), (5, 'hh'), (6, 'habr')]]
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', AsyncMock(return_value=vacancies))
    details, dispatches = [], []

    async def fetch(vacancy, *args, **kwargs):
        details.append(vacancy['id'])
        return 'Synthetic ordinary details'

    async def dispatch(vacancy, *args, **kwargs):
        dispatches.append(vacancy['id'])
        if outcome == 'uncertain':
            return {'ok': False, 'uncertain': True}
        return {'ok': False, 'hh_hard_stop': True, 'hh_recovery_reason': 'Synthetic unproven UI'}

    monkeypatch.setattr(agent.apply_orchestrator, 'fetch_vacancy_details', fetch)
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    if termination == 'cancelled':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(agent.do_search())
    else:
        result = asyncio.run(agent.do_search())
        assert result['hh_recovery']['stopped']
        assert result['funnel']['uncertain'] == (1 if outcome == 'uncertain' else 0)
        assert result['funnel']['failed'] == 0
    assert details == dispatches == ['4']
    assert seen._load()['4']['action'] == expected_seen
    decisions = [event['decision'] for event in read_json_records(config.ANALYTICS_EVENTS_FILE)
                 if event.get('event') == 'decision']
    assert decisions.count(expected_decision) == 1
    assert set(decisions) <= {expected_decision, 'not_processed'}
    assert not any(decision.startswith('apply_failed') for decision in decisions)
    assert '5' not in seen._load() and '6' not in seen._load()
