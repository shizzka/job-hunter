"""Unowned mutations outlive receipts; third-party never enters the HH POST sequence."""
import asyncio

import pytest

from hh.recovery import action_observed
from hh.submit_boundary import RESPONSE_PATH
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from tests.test_hh_recovery_attempt_window import URL, monitored_client, request


@pytest.mark.parametrize('source', ['page', 'worker'])
@pytest.mark.parametrize('method,path', [
    ('POST', RESPONSE_PATH), ('PUT', '/unknown'), ('PATCH', RESPONSE_PATH + '/popup'),
    *[('GET', '/unknown?' + key + '=approved')
      for key in ('resume_id', 'resumeId', 'resumeHash', 'resume', 'letter')],
])
def test_hh_mutation_is_passive_before_first_attempt_then_invalidates_receipt(
        tmp_path, source, method, path):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = request(page if source == 'page' else None, 'https://hh.ru' + path, method)
    context.emit('request', event)
    assert context._hh_action_watch.observations == 1
    assert not context._hh_action_watch.unknown and not monitor.unknown
    retained = []

    async def operation():
        retained.append(client._external_attempt)
        return {'ok': False}

    asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert client._external_attempt is None
    assert repository.get(URL)['status'] == 'failed'
    context.emit('request', event)
    assert retained[0].uncertain and action_observed(client, retained[0])
    assert client._hh_recovery_stop_reason == 'possible_external_action'
    assert repository.get(URL)['status'] == 'uncertain'
    assert repository.get(URL)['owner'] == retained[0].owner
    assert not repository.claim(URL, '', '')


@pytest.mark.parametrize('stage', ['preparing', 'acting', 'finished'])
@pytest.mark.parametrize('host', ['mc.yandex.ru', 'hh.ru.example.invalid'])
@pytest.mark.parametrize('method,path', [
    ('POST', '/watch/123'), ('PUT', RESPONSE_PATH),
    ('POST', RESPONSE_PATH + '/popup'), ('GET', RESPONSE_PATH + '?resume_id=approved'),
])
@pytest.mark.parametrize('flag', ['0', '1'])
def test_third_party_never_counts_as_application_but_unproven_mutation_stops_when_enabled(
        tmp_path, monkeypatch, stage, host, method, path, flag):
    monkeypatch.setenv('HH_PASSIVE_TELEMETRY_ENABLED', flag)
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    retained = []

    def observe():
        context.emit('request', request(page, 'https://' + host + path, method))

    async def operation():
        attempt = client._external_attempt
        retained.append(attempt)
        if stage != 'preparing':
            attempt.begin()
        if stage != 'finished':
            observe()
        return {'ok': True}

    result = asyncio.run(run_native_attempt(client, repository, URL, operation))
    if stage == 'finished':
        observe()
    stops = flag == '1'
    assert bool(result.get('uncertain')) is (stops and stage != 'finished')
    assert result['ok'] is (not stops or stage == 'finished')
    assert retained[0].uncertain is stops
    assert monitor.unknown is stops and context._hh_action_watch.unknown is stops
    assert context._hh_action_watch.observations == 1
    assert context._hh_action_watch.application_post_sequence == 0
    assert retained[0]._hh_application_posts == []
    assert repository.get(URL)['status'] == ('uncertain' if stops else 'completed')


def test_detached_session_retains_late_context_evidence(tmp_path):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')

    async def operation():
        return {'ok': False}

    asyncio.run(run_native_attempt(client, repository, URL, operation))
    client._page = client._context = None
    context.emit('request', request(page, 'https://spb.hh.ru/unknown', 'PUT'))
    assert monitor.unknown and context._hh_action_watch.unknown
    assert repository.get(URL)['status'] == 'uncertain'
