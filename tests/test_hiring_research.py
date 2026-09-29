import json
from datetime import datetime, timezone

import analytics
import config
import hiring_research as research


def report(tmp_path, rows):
    path = tmp_path / 'events.jsonl'
    path.write_text('\n'.join(json.dumps(row) for row in rows))
    return research.summarize(str(path), now=datetime(2026, 9, 28, 15, tzinfo=timezone.utc))


def apply(vid='1', at='2026-09-28T10:00:00+00:00', **extra):
    return dict(event='decision', decision='applied_auto', vacancy_id=vid, source='hh', created_at=at, run_id='a', **extra)


def status(at, bucket, **extra):
    return dict(event='negotiation_observation', vacancy_id='hh:1', source='hh', created_at=at, status_bucket=bucket, **extra)


def test_rejection_interval_and_view_observation(tmp_path):
    result = report(tmp_path, [apply(), status('2026-09-28T10:01:00+00:00', 'pending', status_detail_bucket='pending_viewed'),
                               status('2026-09-28T10:04:00+00:00', 'rejected')])
    assert result['rejection_delays']['<5 мин'] == 1
    assert result['rejection_views']['просмотр замечен'] == 1
    assert result['total'] == 1


def test_long_poll_gap_does_not_claim_exact_delay(tmp_path):
    result = report(tmp_path, [apply(), status('2026-09-28T14:00:00+00:00', 'rejected')])
    assert result['rejection_delays']['интервал пересекает границы'] == 1
    assert result['rejection_views']['просмотр не наблюдался'] == 1


def test_duplicates_dry_run_and_repeat_ambiguity(tmp_path):
    first = apply()
    result = report(tmp_path, [first, first, apply('2', dry_run=True)])
    assert result['total'] == 1
    second = {**first, 'created_at': '2026-09-28T12:00:00+00:00', 'run_id': 'b'}
    result = report(tmp_path, [first, second])
    assert result['total'] == 0 and result['excluded_repeats'] == 1


def test_conversion_and_unknown_historical_features(tmp_path):
    result = report(tmp_path, [apply(submission_mode='manual', resume_variant='qa-v2', has_cover_letter=False),
                               status('2026-09-28T11:00:00+00:00', 'positive'),
                               status('2026-09-28T12:00:00+00:00', 'positive')])
    assert result['groups']['mode']['manual']['positive'] == 1
    assert result['groups']['cover']['доставка неизвестна']['total'] == 1
    assert result['groups']['resume']['qa-v2 · локальная версия неизвестна']['positive'] == 1
    assert result['groups']['screening']['unknown']['total'] == 1
    assert len(list(research.render(result))) == 5


def test_repeated_poll_is_saved_and_profile_state_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    first_file = tmp_path / 'a.jsonl'
    monkeypatch.setattr(config, 'ANALYTICS_EVENTS_FILE', str(first_file))
    monkeypatch.setattr(config, 'ANALYTICS_STATE_FILE', str(tmp_path / 'a.json'))
    analytics._state = None
    analytics.record_negotiation_statuses([{'id': '1', 'status': 'Не просмотрен'}])
    analytics.record_negotiation_statuses([{'id': '1', 'status': 'Не просмотрен'}])
    rows = [json.loads(line) for line in first_file.read_text().splitlines()]
    polls = [row for row in rows if row['event'] == 'negotiation_observation']
    assert len(polls) == 2 and polls[1]['previous_poll_at'] == polls[0]['observed_at_utc']
    monkeypatch.setattr(config, 'ANALYTICS_STATE_FILE', str(tmp_path / 'b.json'))
    assert analytics._load_state().get('last_poll_by_vacancy', {}) == {}


def test_screening_does_not_assume_human_or_store_text(monkeypatch):
    rows = []
    monkeypatch.setattr(analytics, '_append_event', rows.append)
    research.record_screening({'url': 'https://hh.ru/vacancy/123'}, [{'text': 'private', 'is_other': True}])
    assert rows[0]['screening_type'] == 'unknown'
    assert 'private' not in str(rows)


def test_calendar_cohort_and_sources_do_not_mix(tmp_path):
    result = report(tmp_path, [apply(at='2026-08-29T20:59:59+00:00'),
                               {**apply('2'), 'source': 'habr'},
                               {**status('2026-09-28T11:00:00+00:00', 'rejected'), 'vacancy_id': '2'}])
    assert result['total'] == 1
    assert result['outcomes']['unknown'] == 1
    assert not result['rejection_delays']


def test_rejection_bounds_use_last_unchanged_poll(tmp_path):
    result = report(tmp_path, [apply(), status('2026-09-28T10:07:00+00:00', 'pending'),
                               status('2026-09-28T10:10:00+00:00', 'rejected', previous_poll_at='2026-09-28T10:07:00+00:00')])
    assert result['rejection_delays']['5 мин–3 ч'] == 1


def test_generated_letter_never_proves_delivery(tmp_path):
    rows = [apply('1', cover_letter_length=500),
            apply('2', cover_letter_status='confirmed'),
            apply('3', cover_letter_status='submitted_with_application'),
            apply('4', cover_letter_status='unconfirmed')]
    groups = report(tmp_path, rows)['groups']['cover']
    assert groups['доставка неизвестна']['total'] == 1
    assert groups['доставка подтверждена']['total'] == 1
    assert groups['заполнено в форме отклика']['total'] == 1
    assert groups['доставка не подтверждена']['total'] == 1


def test_shadow_checks_render_even_without_applications(tmp_path):
    result = report(tmp_path, [{'event': 'relevance_verification', 'created_at': '2026-09-28T10:00:00+00:00', 'source': 'hh', 'vacancy_id': '1', 'verdict': 'review'}])
    assert result['total'] == 0
    assert result['verifications']['review'] == 1
    assert 'спорно: 1' in list(research.render(result))[0]


def test_same_resume_name_different_local_versions_are_separate(tmp_path):
    result = report(tmp_path, [apply('1', resume_variant='qa', cover_letter_resume_sha256='a'*64),
                               apply('2', resume_variant='qa', cover_letter_resume_sha256='b'*64)])
    assert len(result['groups']['resume']) == 2


def test_unknown_poll_does_not_falsely_narrow_rejection_interval(tmp_path):
    result = report(tmp_path, [apply(), status('2026-09-28T13:59:00+00:00', 'unknown'),
                               status('2026-09-28T14:00:00+00:00', 'rejected', previous_poll_at='2026-09-28T13:59:00+00:00')])
    assert result['rejection_delays']['интервал пересекает границы'] == 1
