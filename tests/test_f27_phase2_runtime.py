"""Per-search selection controls use synthetic profiles and never contact HH."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

import config
import directions
import matcher
import profile
import search_pipeline
import work_conditions
from tests.test_directions import saved


@pytest.mark.parametrize('threshold', [0, 45, 60, 75, 100])
def test_threshold_roundtrip_and_default(tmp_path, threshold):
    value = saved(tmp_path)
    assert value['matcher_threshold'] == 60
    value['matcher_threshold'] = threshold
    directions.save_direction(tmp_path, value)
    assert directions.load_direction(tmp_path, 'qa')['matcher_threshold'] == threshold


@pytest.mark.parametrize('threshold', [-1, 101, True, '60', 60.5, None])
def test_invalid_threshold_cannot_change_search(tmp_path, threshold):
    value = saved(tmp_path)
    before = (tmp_path / 'directions/qa.json').read_bytes()
    value['matcher_threshold'] = threshold
    with pytest.raises(directions.DirectionError):
        directions.save_direction(tmp_path, value)
    assert (tmp_path / 'directions/qa.json').read_bytes() == before


def test_legacy_remote_setting_migrates_each_search_without_read_writes(tmp_path):
    for name in ['qa', 'analyst']:
        saved(tmp_path, name)
        path = tmp_path / f'directions/{name}.json'
        raw = json.loads(path.read_text())
        raw.pop('work_format_version', None)
        path.write_text(json.dumps(raw))
    (tmp_path / 'profile.env').write_text('HH_SEARCH_REMOTE_ONLY=1\n')
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    values = directions.list_directions(tmp_path)
    assert all(v['work_conditions']['allowed_formats'] == ['remote'] for v in values)
    assert before == {p: p.read_bytes() for p in before}
    # Saving an explicit new choice finishes migration; the retired flag cannot undo it.
    values[0]['work_conditions']['allowed_formats'] = ['hybrid']
    directions.save_direction(tmp_path, values[0])
    assert directions.load_direction(tmp_path, values[0]['name'])['work_conditions']['allowed_formats'] == ['hybrid']
    assert directions.load_direction(tmp_path, values[1]['name'])['work_conditions']['allowed_formats'] == ['remote']


@pytest.mark.parametrize('overlay_active', [False, True])
def test_nonactive_named_profile_migrates_inherited_remote_default(tmp_path, monkeypatch, overlay_active):
    monkeypatch.setenv('JOB_HUNTER_HOME', str(tmp_path))
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(tmp_path))
    base = profile.Profile(home_dir=str(tmp_path))
    base.hh.search_remote_only = True
    base.hh.search_profiles = [{'area': 113, 'schedule': ''}]
    monkeypatch.setattr(config, 'SEARCH_PROFILES', [{'area': 113, 'schedule': ''}])
    monkeypatch.setattr(config, '_DIRECTION_BASE_PROFILE', base if overlay_active else None, raising=False)
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', {'name': 'another'} if overlay_active else None, raising=False)
    # An active direction clears this optimization; its saved base is authoritative.
    monkeypatch.setattr(config, 'HH_SEARCH_REMOTE_ONLY', not overlay_active)
    home = tmp_path / 'profiles' / 'inherited'
    home.mkdir(parents=True)
    (home / 'profile.env').write_text('HH_ENABLED=0\n')
    assert profile.load_profile('inherited').hh.search_remote_only is True
    saved(home, count=1)
    path = home / 'directions' / 'qa.json'
    raw = json.loads(path.read_text())
    raw.pop('work_format_version')
    path.write_text(json.dumps(raw))
    directions.set_active_direction(home, 'qa')
    before = {p: p.read_bytes() for p in home.rglob('*') if p.is_file()}
    migrated = profile.load_profile('inherited')
    assert migrated.direction['work_conditions']['allowed_formats'] == ['remote']
    assert migrated.hh.search_profiles and all(p['schedule'] == 'remote' for p in migrated.hh.search_profiles)
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize('formats,schedule', [(['remote'], 'remote'), (['hybrid'], ''),
                                             (['on_site', 'remote'], '')])
def test_hh_format_filter_comes_only_from_search_work_conditions(tmp_path, formats, schedule):
    value = saved(tmp_path)
    value['work_conditions']['allowed_formats'] = formats
    value['search_profiles'] = [{'area': 2, 'schedule': 'remote'}, {'area': 113, 'schedule': 'fullDay'}]
    directions.save_direction(tmp_path, value)
    directions.set_active_direction(tmp_path, 'qa')
    person = profile.Profile(home_dir=str(tmp_path))
    person.hh.search_remote_only = True
    overlaid = directions.overlay_profile(person)
    assert overlaid.hh.search_profiles == [{'area': 2, 'schedule': schedule}, {'area': 113, 'schedule': schedule}]
    assert not overlaid.hh.search_remote_only


def test_explicit_profile_remote_refusal_and_saved_search_choice_win_over_inherited_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HH_SEARCH_REMOTE_ONLY', True)
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', None, raising=False)
    value = saved(tmp_path)
    path = tmp_path / 'directions/qa.json'
    raw = json.loads(path.read_text())
    raw.pop('work_format_version')
    path.write_text(json.dumps(raw))
    (tmp_path / 'profile.env').write_text('HH_SEARCH_REMOTE_ONLY=0\n')
    assert directions.load_direction(tmp_path, 'qa')['work_conditions']['allowed_formats'] == list(work_conditions.FORMAT_LABELS)
    value['work_conditions']['allowed_formats'] = ['hybrid']
    directions.save_direction(tmp_path, value)
    (tmp_path / 'profile.env').write_text('HH_SEARCH_REMOTE_ONLY=1\n')
    assert directions.load_direction(tmp_path, 'qa')['work_conditions']['allowed_formats'] == ['hybrid']


@pytest.mark.parametrize('threshold,score,auto,review', [(75, 74, False, False), (75, 75, True, False),
    (45, 45, False, True), (0, 0, False, True), (60, 59, False, False), (100, 100, True, False)])
def test_selection_threshold_never_lowers_auto_safety(monkeypatch, threshold, score, auto, review):
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', {'matcher_threshold': threshold}, raising=False)
    monkeypatch.setattr(config, 'HH_MATCHER_AUTO_APPLY_MIN_SCORE', 58)
    monkeypatch.setattr(config, 'HH_MATCHER_MANUAL_REVIEW_ENABLED', True)
    result = matcher._apply_auto_apply_threshold({'score': score, 'should_apply': True, 'red_flags': []})
    assert result['should_apply'] is auto
    assert matcher.is_manual_review_candidate(result) is review


@pytest.mark.parametrize('blocked', [{'red_flags': ['unsafe']}, {'guard_flags': ['candidate_claim_overstatement']},
    {'error_kind': 'llm_error'}, {'work_conditions_status': 'review'}, {'score': None}])
def test_custom_zero_threshold_keeps_all_safety_refusals(monkeypatch, blocked):
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', {'matcher_threshold': 0}, raising=False)
    result = {'score': 90, 'should_apply': False, 'red_flags': [], **blocked}
    assert not matcher.is_manual_review_candidate(result)


def test_collection_ignores_retired_profile_remote_filter_with_active_search(monkeypatch):
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', {'name': 'qa'}, raising=False)
    monkeypatch.setattr(config, 'HH_SEARCH_REMOTE_ONLY', True)
    monkeypatch.setattr(config, 'SEARCH_PROFILES', [{'area': 2, 'schedule': ''}])
    monkeypatch.setattr(config, 'SEARCH_QUERIES', ['qa'])
    monkeypatch.setattr(config, 'SEARCH_PAGES', 1)
    monkeypatch.setattr(search_pipeline, 'office_log', AsyncMock())
    client = Mock(is_logged_in=AsyncMock(return_value=True), search_vacancies=AsyncMock(return_value=[]))
    asyncio.run(search_pipeline.collect_hh_vacancies(client))
    client.search_vacancies.assert_awaited_once_with('qa', page=0, area=2, schedule='')


@pytest.mark.parametrize('mode', ['preview', 'confirm', 'auto'])
@pytest.mark.parametrize('threshold,score,visible', [(75, 74, False), (75, 75, True),
    (45, 45, True), (0, 0, True), (100, 99, False), (100, 100, True)])
def test_search_pipeline_threshold_precedes_cards_and_dispatch(tmp_path, monkeypatch, mode, threshold, score, visible):
    import agent
    from hh import negotiation_snapshot
    from tests.test_observability import configure_synthetic_search
    configure_synthetic_search(monkeypatch, tmp_path)
    monkeypatch.setattr(agent, 'is_manual_review_candidate', matcher.is_manual_review_candidate)
    value = saved(tmp_path, count=1)
    value['matcher_threshold'] = threshold
    value = directions.save_direction(tmp_path, value)
    directions.set_active_direction(tmp_path, 'qa')
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', value, raising=False)
    monkeypatch.setattr(config, 'HH_APPLICATION_MODE', mode)
    monkeypatch.setattr(config, 'HH_MATCHER_AUTO_APPLY_MIN_SCORE', 58)
    monkeypatch.setattr(negotiation_snapshot, 'refresh_for_chats', lambda *a: [])
    vacancy = {'id': '42', 'source': 'hh', 'title': 'QA', 'company': 'Example',
               'url': 'https://hh.ru/vacancy/42', 'workFormats': ['REMOTE']}
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', AsyncMock(return_value=[vacancy]))
    monkeypatch.setattr(agent.search_pipeline.filters, 'check_vacancy', lambda v: None)
    monkeypatch.setattr(agent, 'evaluate_vacancy', AsyncMock(return_value={
        'score': score, 'should_apply': True, 'reason': 'Synthetic', 'red_flags': []}))
    dispatch = AsyncMock(return_value={'ok': False, 'already_applied': True})
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    result = asyncio.run(agent.do_search())
    assert dispatch.await_count == int(visible and mode == 'auto' and score >= 58)
    assert agent.notify_needs_manual.await_count == int(visible and (mode != 'auto' or score < 58))
    if not visible:
        assert result['source_stats']['hh']['rejected'] == 1


def test_retired_remote_button_opens_per_search_editor_without_profile_write(monkeypatch):
    from telegram_app.search import TelegramSearch
    bot = TelegramSearch()
    bot._dispatch_direction = AsyncMock()
    asyncio.run(bot._route_search_remote_toggle(1, {'user_id': 1}, '/search_remote_toggle', '', 'qa', None))
    bot._dispatch_direction.assert_awaited_once_with(1, {'user_id': 1}, '/direction_edit', 'work_formats')


def test_retired_global_controls_are_absent_from_every_menu():
    import telegram_bot_ui as ui
    for role in (ui.ROLE_ADMIN, ui.ROLE_USER):
        buttons = [label for rows in ui.reply_menu_layouts(role).values() for row in rows for label in row]
        assert ui.BUTTON_SEARCH_REMOTE not in buttons
        assert ui.BUTTON_ZERO_APPLY not in buttons
