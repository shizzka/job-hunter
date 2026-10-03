"""Version, corruption, concurrency and durability of persisted form claims."""
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from google_forms import drafts
from google_forms.workflow import FormWorkflow
from state_store.google_forms import GoogleFormStateRepository
from state_store import json_store
from tests.test_async_form_workflow import TOKEN, NEXT, preview


def claim_worker(home, barrier, results):
    barrier.wait(timeout=10)
    try:
        FormWorkflow(home).claim(TOKEN)
        results.put(True)
    except ValueError:
        results.put(False)


@pytest.fixture
def flow(tmp_path):
    workflow = FormWorkflow(str(tmp_path))
    workflow.repository.remember(TOKEN, preview(), trim_expired=False)
    return workflow


def test_four_processes_claim_one_attempt(flow):
    ctx = multiprocessing.get_context('spawn')
    barrier, results = ctx.Barrier(4), ctx.Queue()
    workers = [ctx.Process(target=claim_worker, args=(flow.home, barrier, results)) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert not worker.is_alive() and worker.exitcode == 0
    assert sum(results.get(timeout=5) for _ in workers) == 1
    results.close()


def test_thirty_threads_claim_one_attempt(flow):
    barrier = Barrier(30)
    def worker(_):
        barrier.wait(timeout=10)
        try:
            FormWorkflow(flow.home).claim(TOKEN)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=30) as pool:
        assert sum(pool.map(worker, range(30))) == 1


@pytest.mark.parametrize('status', ['submit_in_progress', 'submitted', 'submit_uncertain', 'already_submitted'])
def test_same_form_other_token_cannot_send_again(flow, status):
    _, attempt = flow.claim(TOKEN)
    if status != 'submit_in_progress':
        flow.mark_submitting(TOKEN, attempt)
        flow.finish(TOKEN, attempt, {'ok': status == 'submitted', 'submitted': True})
        if status == 'already_submitted':
            state = flow.repository.load()
            state['items'][TOKEN]['status'] = status
            flow.repository.save(state)
    flow.repository.remember(NEXT, preview(NEXT, preview()['form_url'] + '?usp=sf_link'), trim_expired=False)
    with pytest.raises(ValueError, match='форма уже|проверки'):
        flow.claim(NEXT)


def test_claim_and_uncertain_result_do_not_expire(flow):
    state = flow.repository.load()
    state['items'][TOKEN]['created_at'] = int(time.time())
    flow.repository.save(state)
    _, attempt = flow.claim(TOKEN)
    expired_repo = GoogleFormStateRepository(flow.home, clock=lambda: time.time() + 9 * 86400)
    expired_repo.remember(NEXT, preview(NEXT), trim_expired=True)
    assert TOKEN in expired_repo.load()['items']
    flow.mark_submitting(TOKEN, attempt)
    flow.finish(TOKEN, attempt)
    expired_repo.remember('cccccccccccc', preview('cccccccccccc'), trim_expired=True)
    assert expired_repo.load()['items'][TOKEN]['status'] == 'submit_uncertain'


def test_late_completion_cannot_overwrite_owner_or_reopen_terminal(flow):
    _, attempt = flow.claim(TOKEN)
    before = flow.repository.path.read_bytes()
    assert not flow.finish(TOKEN, 'wrong', {'ok': True})
    assert flow.repository.path.read_bytes() == before
    flow.mark_submitting(TOKEN, attempt)
    assert flow.finish(TOKEN, attempt, {'ok': True, 'submitted': True})
    before = flow.repository.path.read_bytes()
    assert not flow.finish(TOKEN, attempt)
    assert flow.repository.path.read_bytes() == before


def test_publish_recheck_is_one_file_transition_and_keeps_other_items(flow):
    flow.repository.remember('cccccccccccc', preview('cccccccccccc'), trim_expired=False)
    drafts.save_answer(flow.home, TOKEN, 0, 'Manually reviewed', 7)
    item, edits, version = flow.capture(TOKEN)
    successor = preview(NEXT)
    successor['answers'] = list(edits['answers'].values())
    flow.publish_recheck(TOKEN, version, successor)
    state = flow.repository.load()
    assert state['items'][TOKEN]['superseded_by'] == NEXT
    assert state['items'][NEXT]['answers'] == successor['answers']
    assert 'cccccccccccc' in state['items']
    assert drafts.manual_answers(flow.home, TOKEN)
    drafts.supersede(flow.home, TOKEN, NEXT)  # Same successor remains an idempotent no-op.
    with pytest.raises(ValueError, match='новая'):
        drafts.get_draft(flow.home, TOKEN)
    with pytest.raises(ValueError):
        flow.publish_recheck(TOKEN, version, preview('dddddddddddd'))


@pytest.mark.parametrize('change', ['answer', 'source', 'target', 'collision'])
def test_stale_revision_or_changed_target_cannot_publish_successor(flow, change):
    _, _, version = flow.capture(TOKEN)
    detail = preview(NEXT)
    if change == 'answer':
        drafts.save_answer(flow.home, TOKEN, 0, 'Changed', 7)
    elif change == 'source':
        flow.repository.remember(TOKEN, {**preview(), 'source_message': 'Changed context'}, trim_expired=False)
    elif change == 'target':
        detail['form_url'] = 'https://docs.google.com/forms/d/e/other/viewform'
    else:
        flow.repository.remember(NEXT, detail, trim_expired=False)
    before = flow.repository.path.read_bytes()
    with pytest.raises(ValueError):
        flow.publish_recheck(TOKEN, version, detail)
    assert flow.repository.path.read_bytes() == before


@pytest.mark.parametrize('phase', ['preparing', 'submitting'])
def test_failure_before_and_after_click_has_distinct_terminal_outcome(flow, phase):
    _, attempt = flow.claim(TOKEN)
    if phase == 'submitting':
        flow.mark_submitting(TOKEN, attempt)
    flow.finish(TOKEN, attempt)
    item = flow.repository.load()['items'][TOKEN]
    assert item['status'] == ('submit_failed' if phase == 'preparing' else 'submit_uncertain')
    if phase == 'submitting':
        with pytest.raises(ValueError):
            drafts.get_draft(flow.home, TOKEN)


@pytest.mark.parametrize('operation', ['claim', 'mark', 'finish', 'publish'])
@pytest.mark.parametrize('failure', ['replace', 'fsync'])
def test_disk_failure_preserves_old_state_and_blocks_next_action(flow, monkeypatch, operation, failure):
    attempt = None
    _, _, version = flow.capture(TOKEN)
    if operation in {'mark', 'finish'}:
        _, attempt = flow.claim(TOKEN)
    before = flow.repository.path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('Synthetic persistence failure')
    monkeypatch.setattr(json_store.os, failure, fail)
    with pytest.raises(OSError):
        if operation == 'claim':
            flow.claim(TOKEN)
        elif operation == 'mark':
            flow.mark_submitting(TOKEN, attempt)
        elif operation == 'finish':
            flow.finish(TOKEN, attempt)
        else:
            flow.publish_recheck(TOKEN, version, preview(NEXT))
    assert flow.repository.path.read_bytes() == before
    assert not list(Path(flow.home).glob('*.tmp'))


@pytest.mark.parametrize('name', ['google_form_previews.json', 'google_form_edits.json'])
@pytest.mark.parametrize('content', [b'{', b'[]', b'\xff'])
def test_corruption_prevents_claim_and_keeps_bytes(flow, name, content):
    path = Path(flow.home) / name
    path.write_bytes(content)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='restore'):
            flow.claim(TOKEN)
        assert path.read_bytes() == content


def test_claim_refuses_stale_revision_before_mutation(flow):
    _, _, version = flow.capture(TOKEN)
    drafts.save_answer(flow.home, TOKEN, 0, 'Later edit', 7)
    before = flow.repository.path.read_bytes()
    with pytest.raises(ValueError, match='измен'):
        flow.claim(TOKEN, expected_revision=version)
    assert flow.repository.path.read_bytes() == before


def test_preclick_check_detects_noncooperating_writer(flow):
    _, attempt = flow.claim(TOKEN)
    state = flow.repository.load()
    state['items'][TOKEN]['answers'][0]['answer'] = 'Changed by external writer'
    flow.repository.save(state)  # Explicit full replacement is not a cooperating mutation.
    with pytest.raises(ValueError, match='измен'):
        flow.mark_submitting(TOKEN, attempt)


def test_claim_writes_private_file_and_keeps_metadata(flow):
    state = flow.repository.load()
    state['metadata'] = {'retained': True}
    flow.repository.save(state)
    flow.claim(TOKEN)
    assert flow.repository.load()['metadata'] == {'retained': True}
    assert flow.repository.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('operation', ['claim', 'mark'])
def test_directory_fsync_uncertainty_keeps_blocking_claim(flow, monkeypatch, operation):
    import os
    import stat
    attempt = None
    if operation == 'mark':
        _, attempt = flow.claim(TOKEN)
    original = json_store.os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('Synthetic post-publication directory fsync failure')
        return original(fd)
    monkeypatch.setattr(json_store.os, 'fsync', fail_directory)
    with pytest.raises(OSError):
        if operation == 'claim':
            flow.claim(TOKEN)
        else:
            flow.mark_submitting(TOKEN, attempt)
    item = flow.repository.load()['items'][TOKEN]
    assert item['status'] == 'submit_in_progress'
    assert item['submission']['phase'] == ('preparing' if operation == 'claim' else 'submitting')
    with pytest.raises(ValueError):
        flow.claim(TOKEN)


def test_publish_serialization_failure_keeps_original_and_no_successor(flow):
    _, _, version = flow.capture(TOKEN)
    before = flow.repository.path.read_bytes()
    detail = {**preview(NEXT), 'metadata': object()}
    with pytest.raises(TypeError):
        flow.publish_recheck(TOKEN, version, detail)
    assert flow.repository.path.read_bytes() == before
    assert NEXT not in flow.repository.load()['items']


@pytest.mark.parametrize('change', ['phase', 'status', 'time', 'revision'])
def test_corrupt_claim_shape_cannot_be_reinterpreted_as_retryable_preview(flow, change):
    import json
    flow.claim(TOKEN)
    state = flow.repository.load()
    item = state['items'][TOKEN]
    if change == 'phase':
        item['submission']['phase'] = ['submitting']
    elif change == 'status':
        item['status'] = 'preview'
    elif change == 'time':
        item['submission']['started_at'] = True
    else:
        item['submission']['revision'] = 'x' * 64
    content = json.dumps(state).encode()
    flow.repository.path.write_bytes(content)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='restore'):
            flow.claim(TOKEN)
        assert flow.repository.path.read_bytes() == content
