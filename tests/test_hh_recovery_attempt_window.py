"""Unowned observations respect pending claims and retained attempt history."""
import asyncio
from types import SimpleNamespace

import pytest

from hh.recovery import action_observed, monitor_page
from hh.submit_boundary import RESPONSE_PATH
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from tests.test_hh_late_outcome_adversarial_e import Emitter


URL = 'https://hh.ru/vacancy/42'


def monitored_client():
    context, page = Emitter(), Emitter()
    page.context = context
    client = SimpleNamespace(_page=page, _context=context)
    monitor = monitor_page(client, page)
    return client, context, page, monitor


def request(page, url, method='POST'):
    return SimpleNamespace(method=method, url=url, frame=SimpleNamespace(page=page),
                           headers={}, post_data=None if method in {'GET', 'HEAD'} else 'unapproved')


@pytest.mark.parametrize('stage', ['idle', 'preparing', 'finished'])
@pytest.mark.parametrize('surface', ['page', 'unmonitored', 'worker', 'event', 'malformed'])
def test_background_observations_respect_unproven_attempt_window(tmp_path, stage, surface):
    client, context, page, monitor = monitored_client()
    watch = context._hh_action_watch
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    retained = []
    requires_stop = stage != 'idle' and surface in {'event', 'malformed'}

    def observe():
        if surface == 'event':
            monitor.mark_unknown()
        elif surface == 'malformed':
            context.emit('request', object())
        else:
            source = page if surface == 'page' else Emitter() if surface == 'unmonitored' else None
            context.emit('request', request(source, 'https://mc.yandex.ru/watch/123'))
        assert watch.observations == 1
        assert monitor.observations == (1 if surface in {'page', 'event'} else 0)
        assert watch.unknown is requires_stop
        assert monitor.unknown is (requires_stop and surface == 'event')
        if requires_stop:
            assert client._hh_recovery_stop_reason == 'possible_external_action'
            assert client._hh_recovery_uncertain
        else:
            assert not getattr(client, '_hh_recovery_stop_reason', '')
            assert not getattr(client, '_hh_recovery_uncertain', False)

    async def operation():
        attempt = client._external_attempt
        retained.append(attempt)
        if stage == 'preparing':
            observe()
            assert attempt.uncertain is requires_stop
            assert action_observed(client, attempt) is requires_stop
        else:
            attempt.begin()
        return {'ok': True}

    if stage == 'idle':
        observe()
    else:
        result = asyncio.run(run_native_attempt(client, repository, URL, operation))
        if stage == 'preparing' and requires_stop:
            assert not result['ok'] and result['uncertain']
        else:
            assert result['ok'] and not result.get('uncertain')
        if stage == 'finished':
            observe()
            assert retained[0].acting and client._external_attempt is None
        assert retained[0].uncertain is requires_stop
        assert repository.get(URL)['status'] == ('uncertain' if requires_stop else 'completed')
        assert repository.get(URL)['owner'] == retained[0].owner
        assert not repository.claim(URL, '', '')


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH'])
@pytest.mark.parametrize('path', [RESPONSE_PATH, RESPONSE_PATH + '/popup'])
@pytest.mark.parametrize('source', ['page', 'worker'])
def test_response_creation_outside_window_stays_fail_closed(tmp_path, method, path, source):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    retained = []

    async def operation():
        retained.append(client._external_attempt)
        return {'ok': False}

    asyncio.run(run_native_attempt(client, repository, URL, operation))
    context.emit('request', request(page if source == 'page' else None,
                                   'https://hh.ru' + path, method))
    assert context._hh_action_watch.unknown
    assert client._hh_recovery_stop_reason == 'possible_external_action'
    assert client._hh_recovery_uncertain and retained[0].uncertain
    assert repository.get(URL)['status'] == 'uncertain'
    assert repository.get(URL)['owner'] == retained[0].owner
    assert action_observed(client, retained[0])
    assert not repository.claim(URL, '', '')


@pytest.mark.parametrize('surface', ['request', 'event', 'malformed'])
def test_unknown_inside_window_remains_sticky_after_window_closes(tmp_path, surface):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    retained = []

    async def operation():
        attempt = client._external_attempt
        retained.append(attempt)
        attempt.begin()
        if surface == 'request':
            context.emit('request', request(page, 'https://hh.ru/analytics/pageview'))
        elif surface == 'malformed':
            context.emit('request', object())
        else:
            monitor.mark_unknown()
        return {'ok': True}

    result = asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert not result['ok'] and result['uncertain']
    assert monitor.unknown is (surface != 'malformed')
    assert context._hh_action_watch.unknown and action_observed(client, retained[0])
    monitor.mark_unknown()
    assert retained[0].uncertain and monitor.unknown
    assert context._hh_action_watch.unknown
    assert client._hh_recovery_uncertain
    assert repository.get(URL)['status'] == 'uncertain'
    assert not repository.claim(URL, '', '')
