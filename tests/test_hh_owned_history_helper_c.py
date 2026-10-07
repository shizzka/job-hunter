"""Independent exact-owned history checks, with no browser/network transport."""
import asyncio
import socket
from types import SimpleNamespace

import pytest

import apply_orchestrator
from hh.recovery import monitor_page
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from tests.test_hh_recovery_adversarial_e import Emitter

OLD = 'https://hh.ru/vacancy/4'
CURRENT = 'https://hh.ru/vacancy/5'
VACANCY = {'source': 'hh', 'url': OLD}


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Owned history tests must not access the network')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)


async def history(tmp_path, *, old_ok=False, replacement=False):
    context, page = Emitter(), Emitter()
    page.context = context
    client = SimpleNamespace(_context=context, _page=page)
    monitor_page(client, page)
    repository = NativeApplyRepository(tmp_path / 'synthetic-cookies.json', 'hh')
    old_result = {'ok': old_ok, 'message': 'Known synthetic owned receipt'}
    async def old_operation():
        return dict(old_result)
    returned = await run_native_attempt(client, repository, OLD, old_operation)
    assert returned == old_result
    captured = page._hh_action_monitor.last_attempt
    if replacement:
        next_page = Emitter()
        next_page.context = context
        client._page = next_page
        monitor_page(client, next_page)
    return client, context, repository, captured, returned


def approve_current_post(client, context, current):
    current.page._hh_request_approval = {
        'owner': current.owner, 'used': False, 'url': 'https://hh.ru/applicant/vacancy_response',
        'method': 'POST', 'enctype': 'application/x-www-form-urlencoded',
        'entries': [['vacancyId', '5'], ['resumeId', 'synthetic-exact']],
    }
    request = SimpleNamespace(method='POST', url='https://hh.ru/applicant/vacancy_response',
        frame=SimpleNamespace(page=current.page),
        headers={'content-type': 'application/x-www-form-urlencoded'},
        post_data='vacancyId=5&resumeId=synthetic-exact')
    context.emit('request', request)
    assert current.page._hh_action_monitor.owners == {current.owner}
    assert not context._hh_action_watch.unknown


@pytest.mark.parametrize('old_ok', [False, True])
@pytest.mark.parametrize('replacement', [False, True])
@pytest.mark.parametrize('phase', ['preparing', 'acting', 'approved_observed'])
def test_known_prior_receipt_survives_exact_owned_current_attempt(tmp_path, old_ok, replacement, phase):
    async def run():
        client, context, repository, captured, result = await history(
            tmp_path, old_ok=old_ok, replacement=replacement)
        observations = []
        async def current_operation():
            current = client._external_attempt
            assert current is not captured
            assert current.page._hh_action_monitor.attempts[current.owner] is current
            assert captured.page._hh_action_monitor.attempts[captured.owner] is captured
            if phase != 'preparing':
                current.begin()
            if phase == 'approved_observed':
                approve_current_post(client, context, current)
            before = repository.path.read_bytes()
            reconciled = apply_orchestrator.apply_result_with_current_uncertainty(
                VACANCY, client, result, owned_attempt=captured)
            observations.append(reconciled)
            assert reconciled == result
            assert repository.path.read_bytes() == before
            await asyncio.sleep(0)
            return {'ok': phase != 'preparing', 'message': 'Known current outcome'}
        returned = await run_native_attempt(client, repository, CURRENT, current_operation)
        assert not returned.get('uncertain')
        assert observations == [result]
        assert repository.get(OLD)['status'] == ('completed' if old_ok else 'failed')
    asyncio.run(run())


@pytest.mark.parametrize('replacement', [False, True])
def test_owned_prior_durable_uncertainty_is_read_while_next_owner_is_active(tmp_path, replacement):
    async def run():
        client, context, repository, captured, result = await history(tmp_path, replacement=replacement)
        async def current_operation():
            current = client._external_attempt
            repository.transition(captured.url, captured.owner, 'uncertain')
            assert not captured.uncertain and not context._hh_action_watch.unknown
            before = repository.path.read_bytes()
            reconciled = apply_orchestrator.apply_result_with_current_uncertainty(
                VACANCY, client, result, owned_attempt=captured)
            assert reconciled['uncertain'] and not reconciled['ok']
            assert repository.path.read_bytes() == before
            assert repository.get(CURRENT)['owner'] == current.owner
            assert repository.get(CURRENT)['status'] == 'preparing'
            return {'ok': False}
        await run_native_attempt(client, repository, CURRENT, current_operation)
        assert not repository.claim(OLD, '', '')
    asyncio.run(run())


@pytest.mark.parametrize('corruption', [
    'captured_registry_missing', 'captured_registry_replaced', 'captured_monitor_session',
    'captured_monitor_page', 'current_registry_missing', 'current_registry_replaced',
    'current_monitor_session', 'watch_session', 'watch_context', 'watch_current_mapping',
    'watch_captured_mapping', 'current_client', 'current_context', 'current_page',
    'client_context_restarted', 'current_receipt_owner', 'current_receipt_terminal',
    'unrelated_observed_owner', 'captured_observed_owner', 'captured_receipt_owner',
    'captured_monitor_unknown', 'current_observed_while_preparing', 'captured_monitor_missing',
])
def test_history_ownership_corruption_cannot_preserve_known_result(tmp_path, corruption):
    async def run():
        client, context, repository, captured, result = await history(tmp_path, old_ok=True, replacement=True)
        async def current_operation():
            current = client._external_attempt
            old_monitor = captured.page._hh_action_monitor
            current_monitor = current.page._hh_action_monitor
            watch = context._hh_action_watch
            if corruption == 'captured_registry_missing': old_monitor.attempts.pop(captured.owner)
            elif corruption == 'captured_registry_replaced': old_monitor.attempts[captured.owner] = object()
            elif corruption == 'captured_monitor_session': old_monitor.session = object()
            elif corruption == 'captured_monitor_page': old_monitor.page = object()
            elif corruption == 'current_registry_missing': current_monitor.attempts.pop(current.owner)
            elif corruption == 'current_registry_replaced': current_monitor.attempts[current.owner] = object()
            elif corruption == 'current_monitor_session': current_monitor.session = object()
            elif corruption == 'watch_session': watch.session = object()
            elif corruption == 'watch_context': watch.context = object()
            elif corruption == 'watch_current_mapping': watch.pages[current.page] = object()
            elif corruption == 'watch_captured_mapping': watch.pages[captured.page] = object()
            elif corruption == 'current_client': current.client = object()
            elif corruption == 'current_context': current.context = object()
            elif corruption == 'current_page': current.page = object()
            elif corruption == 'client_context_restarted': client._context = Emitter()
            elif corruption == 'current_receipt_owner':
                def foreign(state): state[repository.key(CURRENT)]['owner'] = 'f' * 32
                repository.store.update(foreign)
            elif corruption == 'current_receipt_terminal':
                repository.transition(CURRENT, current.owner, 'failed')
            elif corruption == 'unrelated_observed_owner': current_monitor.owners.add('unrelated')
            elif corruption == 'captured_observed_owner': old_monitor.owners.add(captured.owner)
            elif corruption == 'captured_monitor_unknown': old_monitor.unknown = True
            elif corruption == 'captured_monitor_missing': captured.page._hh_action_monitor = None
            elif corruption == 'current_observed_while_preparing': current_monitor.owners.add(current.owner)
            elif corruption == 'captured_receipt_owner':
                def foreign(state): state[repository.key(OLD)]['owner'] = 'f' * 32
                repository.store.update(foreign)
            before = repository.path.read_bytes()
            reconciled = apply_orchestrator.apply_result_with_current_uncertainty(
                VACANCY, client, result, owned_attempt=captured)
            assert reconciled.get('uncertain') and not reconciled['ok']
            assert repository.path.read_bytes() == before
            return {'ok': False}
        await run_native_attempt(client, repository, CURRENT, current_operation)
    asyncio.run(run())


@pytest.mark.parametrize('target', ['captured', 'current'])
def test_foreign_history_never_reads_or_writes_foreign_repository(tmp_path, target):
    async def run():
        client, context, repository, captured, result = await history(tmp_path, old_ok=True)
        touched = []
        class ForeignRepository:
            source = 'hh'
            def key(self, url):
                touched.append(('key', url))
                raise AssertionError('Foreign Native repository must not be accessed')
            def get(self, url):
                touched.append(('get', url))
                raise AssertionError('Foreign Native repository must not be accessed')
            def transition(self, *args, **kwargs):
                touched.append(('transition', args))
                raise AssertionError('Foreign Native repository must not be accessed')
        async def current_operation():
            current = client._external_attempt
            foreign = captured if target == 'captured' else current
            old_client, old_repository = foreign.client, foreign.repository
            foreign.client, foreign.repository = object(), ForeignRepository()
            before = repository.path.read_bytes()
            reconciled = apply_orchestrator.apply_result_with_current_uncertainty(
                VACANCY, client, result, owned_attempt=captured)
            assert reconciled.get('uncertain') and not reconciled['ok']
            assert touched == []
            assert repository.path.read_bytes() == before
            foreign.client, foreign.repository = old_client, old_repository
            return {'ok': False}
        await run_native_attempt(client, repository, CURRENT, current_operation)
    asyncio.run(run())


@pytest.mark.parametrize('old_ok', [False, True])
def test_known_historical_page_receipt_without_active_owner_remains_known(tmp_path, old_ok):
    async def run():
        client, context, repository, captured, result = await history(tmp_path, old_ok=old_ok, replacement=True)
        assert client._external_attempt is None
        before = repository.path.read_bytes()
        reconciled = apply_orchestrator.apply_result_with_current_uncertainty(
            VACANCY, client, result, owned_attempt=captured)
        assert reconciled == result
        assert repository.path.read_bytes() == before
    asyncio.run(run())


@pytest.mark.parametrize('corruption', [
    'registry_missing', 'registry_replaced', 'monitor_session', 'monitor_page', 'page_context',
    'unknown', 'unacknowledged_owner', 'watch_context', 'watch_mapping', 'client_context_restarted',
    'captured_client_foreign', 'monitor_missing', 'current_monitor_missing',
    'current_page_unregistered', 'current_page_foreign_context', 'watch_missing',
])
def test_historical_page_requires_exact_registry_without_active_owner(tmp_path, corruption):
    async def run():
        client, context, repository, captured, result = await history(tmp_path, old_ok=True, replacement=True)
        monitor, watch = captured.page._hh_action_monitor, context._hh_action_watch
        if corruption == 'registry_missing': monitor.attempts.pop(captured.owner)
        elif corruption == 'registry_replaced': monitor.attempts[captured.owner] = object()
        elif corruption == 'monitor_session': monitor.session = object()
        elif corruption == 'monitor_page': monitor.page = object()
        elif corruption == 'page_context': captured.page.context = object()
        elif corruption == 'unknown': monitor.unknown = True
        elif corruption == 'unacknowledged_owner': monitor.owners.add(captured.owner)
        elif corruption == 'watch_context': watch.context = object()
        elif corruption == 'watch_mapping': watch.pages[captured.page] = object()
        elif corruption == 'client_context_restarted': client._context = Emitter()
        elif corruption == 'captured_client_foreign': captured.client = object()
        elif corruption == 'monitor_missing': captured.page._hh_action_monitor = None
        elif corruption == 'current_monitor_missing': client._page._hh_action_monitor = None
        elif corruption in {'current_page_unregistered', 'current_page_foreign_context'}:
            foreign = Emitter()
            foreign.context = context if corruption == 'current_page_unregistered' else Emitter()
            client._page = foreign
        elif corruption == 'watch_missing': context._hh_action_watch = None
        before = repository.path.read_bytes()
        reconciled = apply_orchestrator.apply_result_with_current_uncertainty(
            VACANCY, client, result, owned_attempt=captured)
        assert reconciled.get('uncertain') and not reconciled['ok']
        assert repository.path.read_bytes() == before
    asyncio.run(run())


@pytest.mark.parametrize('replacement', [False, True])
@pytest.mark.parametrize('active', [False, True])
def test_missing_captured_page_monitor_is_ownership_loss_even_when_watch_retains_registry(tmp_path, replacement, active):
    async def run():
        client, context, repository, captured, result = await history(
            tmp_path, old_ok=True, replacement=replacement)
        retained_monitor = captured.page._hh_action_monitor
        async def recheck():
            captured.page._hh_action_monitor = None
            assert context._hh_action_watch.pages[captured.page] is retained_monitor
            assert retained_monitor.attempts[captured.owner] is captured
            before = repository.path.read_bytes()
            reconciled = apply_orchestrator.apply_result_with_current_uncertainty(
                VACANCY, client, result, owned_attempt=captured)
            assert reconciled.get('uncertain') and not reconciled['ok']
            assert repository.path.read_bytes() == before
            return {'ok': False}
        if active:
            await run_native_attempt(client, repository, CURRENT, recheck)
        else:
            await recheck()
    asyncio.run(run())
