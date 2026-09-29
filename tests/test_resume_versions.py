import json

import analytics
import config
import manual_apply_queue
import resume_versions


def test_actual_inputs_are_hashed_and_stages_do_not_overwrite():
    vacancy = {}
    resume_versions.record_input(vacancy, 'version one', 'matcher')
    first = resume_versions.payload(vacancy)['matcher_resume_sha256']
    resume_versions.record_input(vacancy, 'version two', 'cover_letter')
    result = resume_versions.payload(vacancy)
    assert result['matcher_resume_sha256'] == first
    assert result['cover_letter_resume_sha256'] != first
    assert 'version one' not in json.dumps(result)


def test_missing_resume_is_not_a_version():
    vacancy = {}
    resume_versions.record_input(vacancy, '(Резюме не найдено — заполни файл)', 'matcher')
    result = resume_versions.payload(vacancy)
    assert result['matcher_resume_sha256'] is None
    assert result['matcher_resume_status'] == 'missing'


def test_queue_preserves_matcher_input_until_later_cover_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MANUAL_APPLY_QUEUE_FILE', str(tmp_path / 'queue.json'))
    monkeypatch.setattr(manual_apply_queue, '_queue_path', lambda profile_name=None: tmp_path / 'queue.json')
    vacancy = {'id': '1'}
    resume_versions.record_input(vacancy, 'old resume', 'matcher')
    item = manual_apply_queue.create_candidate(vacancy, {}, profile_name='qa')
    restored = manual_apply_queue.get_candidate(item['token'])['vacancy']
    resume_versions.record_input(restored, 'edited resume', 'cover_letter')
    result = resume_versions.payload(restored)
    assert result['matcher_resume_sha256'] != result['cover_letter_resume_sha256']


def test_decision_persists_versions_not_text(tmp_path, monkeypatch):
    events = tmp_path / 'events.jsonl'
    monkeypatch.setattr(config, 'ANALYTICS_EVENTS_FILE', str(events))
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    vacancy = {'id': '1', '_requested_resume': {'id': 'resume-id'}}
    resume_versions.record_input(vacancy, 'private resume contents', 'cover_letter')
    analytics.record_decision(run_id='r', vacancy=vacancy, decision='applied_auto')
    event = json.loads(events.read_text())
    assert event['requested_resume_id'] == 'resume-id'
    assert len(event['cover_letter_resume_sha256']) == 64
    assert 'private resume contents' not in events.read_text()
