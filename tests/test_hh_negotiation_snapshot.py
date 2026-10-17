"""Synthetic state only. Every HTTP request uses httpx.MockTransport."""
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from hh import negotiation_snapshot as ns


VACANCY_ATTRIBUTES = {
    'vacancy_work_experience': 'between1And3',
    'vacancy_area_id': 1, 'vacancy_area_name': 'Москва',
    'vacancy_work_formats': ['REMOTE', 'ON_SITE', 'HYBRID'],
    'vacancy_salary_from': 100000, 'vacancy_salary_to': 200000,
    'vacancy_salary_currency': 'RUR', 'vacancy_salary_gross': False,
    'vacancy_employment': 'full', 'vacancy_professional_role_ids': [96, 165],
}


def html(state):
    return '<template id="HH-Lux-InitialState">' + json.dumps(state) + '</template>'


def state(page=0, pages=1, *, archived=False):
    return {
        'applicantNegotiations': {
            'total': pages, 'pageCount': str(pages), 'paging': {'page': page},
            'filterInUse': 'archived' if archived else 'active',
            'topicList': [{'id': 100 + page, 'chatId': 5700000002, 'vacancyId': 42, 'employerId': 8,
                           'employerManagerId': 9, 'resumeId': 10, 'viewedByOpponent': True,
                           'archived': archived, 'creationTimeMillis': 1000,
                           'lastEmployerState': None, 'lastState': 'RESPONSE',
                           'employerSubState': 'NEW', 'lastFunnelStage': 'SCREENING',
                           'employerManager': {'name': 'PRIVATE RECRUITER'},
                           'text': 'PRIVATE MESSAGE', 'letter': 'PRIVATE LETTER',
                           'contacts': {'email': 'PRIVATE CONTACT'},
                           'unknown': {'cookie': 'PRIVATE COOKIE'}}],
        },
        'applicantEmployerPoliteness': {'employerPolitenessIndexes': {} if archived else {
            '8': {'employerId': 8, 'allTopicCount': 30, 'readTopicPercent': 80,
                  'replyTotalWorkingTimeDays': 2, 'name': 'PRIVATE RECRUITER'},
            '88': {'allTopicCount': 999},
        }},
        'applicantEmployerManagersActivity': [] if archived else [
            {'@managerHhid': 9, '@inactiveMinutes': 999},
            {'@managerHhid': '99', '@inactiveMinutes': '90', 'inactive_days': 1,
             'trl_code': 'online', 'name': 'PRIVATE RECRUITER'}],
        'applicantNegotiationsCounters': {'total': {'all': pages, 'archived': 0},
                                         'new': {'all': 0}, 'letter': 'PRIVATE LETTER'},
        'vacanciesShort': {'vacanciesList': [{
            'id': 42, 'name': 'Synthetic vacancy', 'creationTime': 100,
            'publicationTime': 200, 'closedForApplicants': False,
            'workExperience': 'between1And3',
            'area': {'id': 1, 'name': 'Москва', 'managerName': 'PRIVATE NAME'},
            'workFormats': ['REMOTE', 'ON_SITE', 'HYBRID'],
            'compensation': {'from': 100000, 'to': 200000, 'currencyCode': 'RUR',
                             'gross': False, 'managerName': 'PRIVATE NAME'},
            'employment': 'full', 'professionalRoleIds': [96, 165],
            'employerManager': {'@managerId': '9', '@hhid': 99, '@userId': 123,
                                '@firstName': 'PRIVATE FIRST', '@lastName': 'PRIVATE LAST'},
            'company': {'id': 8, 'visibleName': 'Synthetic company', 'name': 'Fallback company',
                        '@trusted': True, 'accreditedITEmployer': True,
                        'contacts': 'PRIVATE CONTACT'}, 'letter': 'PRIVATE LETTER'}]},
        'captchaConfig': {'enabled': True},
    }


def rows(tmp_path):
    return [json.loads(line) for line in (tmp_path / 'negotiation_snapshots.jsonl').read_text().splitlines()]


def transport(tmp_path, monkeypatch, handler):
    cookie_path = tmp_path / 'hh_cookies.json'
    cookie_path.write_text(json.dumps([{'name': 'session', 'value': 'synthetic', 'domain': '.hh.ru'}]))
    calls, pauses = [], []
    real_client = ns._authenticated_client

    def factory(path, *, cookies):
        client = real_client(path, cookies=cookies)
        client.close()
        def dispatch(request):
            assert request.method == 'GET'
            calls.append(request)
            return handler(request)
        return httpx.Client(transport=httpx.MockTransport(dispatch), cookies=client.cookies,
                            follow_redirects=True)
    monkeypatch.setattr(ns, '_authenticated_client', factory)

    def collect(**kwargs):
        return ns.collect(home_dir=tmp_path, cookies_file=cookie_path,
                          sleep=pauses.append, uniform=lambda low, high: (low + high) / 2, **kwargs)
    return collect, calls, pauses


def test_parse_active_joins_and_public_vacancy_fields():
    result = ns.parse_page(html(state()))
    assert result['ok']
    row = result['records'][0]
    assert row['employer_politeness'] == {'allTopicCount': 30, 'readTopicPercent': 80,
                                         'replyTotalWorkingTimeDays': 2}
    assert row['manager_activity'] == {'inactive_days': 1, 'inactiveMinutes': 90, 'trl_code': 'online'}
    assert {key: row[key] for key in ns.VACANCY_FIELDS} == {
        'vacancy_name': 'Synthetic vacancy', 'company_id': 8, 'company_name': 'Synthetic company',
        'company_trusted': True, 'company_accredited_it': True,
        'vacancy_creation_time': 100, 'vacancy_publication_time': 200,
        'closed_for_applicants': False, **VACANCY_ATTRIBUTES}


@pytest.mark.parametrize('archived', [False, True])
def test_vacancy_attributes_in_jsonl(tmp_path, monkeypatch, archived):
    payload = state(archived=archived)
    vacancy = payload['vacanciesShort']['vacanciesList'][0]
    vacancy['publicationTime'] = {'@timestamp': 1700000000000,
                                 '$': '2023-11-14T22:13:20Z', 'name': 'PRIVATE NAME'}
    collect, _, _ = transport(tmp_path, monkeypatch,
        lambda req: httpx.Response(200, text=html(payload)))
    assert collect(archived=archived)['complete']
    row = rows(tmp_path)[0]
    assert {key: row[key] for key in VACANCY_ATTRIBUTES} == VACANCY_ATTRIBUTES
    assert row['vacancy_publication_time'] == 1700000000000
    assert set(row) == set(ns.TOPIC_FIELDS + ns.VACANCY_FIELDS) | {
        'snapshot_id', 'fetched_at', 'filter', 'page', 'employer_politeness', 'manager_activity'}
    assert 'PRIVATE' not in json.dumps(rows(tmp_path))


@pytest.mark.parametrize('publication,expected', [
    ({'$': '2023-11-14T22:13:20Z'}, '2023-11-14T22:13:20Z'),
    ({'@timestamp': {}, '$': '2023-11-14T22:13:20Z'}, '2023-11-14T22:13:20Z'),
    ({'@timestamp': 0, '$': 'unused'}, 0),
    ({'@timestamp': {'name': 'PRIVATE NAME'}, '$': []}, None),
    ({'name': 'PRIVATE NAME'}, None), ([], None), (None, None), (200, 200),
])
def test_publication_time_known_scalars(publication, expected):
    payload = state()
    payload['vacanciesShort']['vacanciesList'][0]['publicationTime'] = publication
    row = ns.parse_page(html(payload))['records'][0]
    assert row['vacancy_publication_time'] == expected
    assert 'PRIVATE' not in json.dumps(row)


def test_vacancy_xml_attributes_and_employment_form():
    payload = state()
    vacancy = payload['vacanciesShort']['vacanciesList'][0]
    vacancy.update(area={'@id': '2', '$': 'Санкт-Петербург'},
                   employment=None, employmentForm='PART_TIME',
                   compensation={'@from': 100, '@to': 200,
                                 '@currencyCode': 'RUR', '@gross': True})
    row = ns.parse_page(html(payload))['records'][0]
    assert {key: row[key] for key in VACANCY_ATTRIBUTES} == dict(
        VACANCY_ATTRIBUTES, vacancy_area_id='2', vacancy_area_name='Санкт-Петербург',
        vacancy_employment='PART_TIME', vacancy_salary_from=100,
        vacancy_salary_to=200, vacancy_salary_gross=True)


@pytest.mark.parametrize('missing', [False, True])
def test_unknown_or_missing_vacancy_attributes_are_null(missing):
    payload = state()
    vacancy = payload['vacanciesShort']['vacanciesList'][0]
    fields = ('workExperience', 'area', 'workFormats', 'compensation',
              'employment', 'employmentForm', 'professionalRoleIds')
    for field in fields:
        if missing:
            vacancy.pop(field, None)
        else:
            vacancy[field] = {'name': 'PRIVATE NAME', 'unknown': ['PRIVATE NAME']}
    if not missing:
        vacancy['area'] = {'id': {}, 'name': ['PRIVATE NAME']}
        vacancy['compensation'] = {key: {'name': 'PRIVATE NAME'} for key in (
            'from', 'to', 'currencyCode', 'gross')}
    row = ns.parse_page(html(payload))['records'][0]
    assert {key: row[key] for key in VACANCY_ATTRIBUTES} == dict.fromkeys(VACANCY_ATTRIBUTES)
    assert 'PRIVATE' not in json.dumps(row)


@pytest.mark.parametrize('formats,roles,expected_formats,expected_roles', [
    ([], [], [], []),
    (['REMOTE', {'name': 'PRIVATE NAME'}, ['PRIVATE NAME'], 1, True],
     [96, True, '165', {'name': 'PRIVATE NAME'}, [1], 2.5], ['REMOTE'], [96]),
    (['REMOTE'] * 101, [96] * 101, None, None),
])
def test_vacancy_lists_are_short_and_typed(formats, roles, expected_formats, expected_roles):
    payload = state()
    payload['vacanciesShort']['vacanciesList'][0].update(
        workFormats=formats, professionalRoleIds=roles)
    row = ns.parse_page(html(payload))['records'][0]
    assert row['vacancy_work_formats'] == expected_formats
    assert row['vacancy_professional_role_ids'] == expected_roles
    assert 'PRIVATE' not in json.dumps(row)


@pytest.mark.parametrize('value,expected', [
    (5700000002, 5700000002), ('5700000002', 5700000002),
    (None, None), (True, None), ({'id': 1}, None), ('private', None),
])
def test_chat_id_retains_only_numbers(value, expected):
    payload = state()
    payload['applicantNegotiations']['topicList'][0]['chatId'] = value
    row = ns.parse_page(html(payload))['records'][0]
    assert row['chatId'] == expected


@pytest.mark.parametrize('missing', ['bridge', 'manager_id', 'hhid', 'activity', 'mismatch'])
def test_manager_activity_requires_complete_bridge(missing):
    payload = state()
    vacancy = payload['vacanciesShort']['vacanciesList'][0]
    if missing == 'bridge':
        del vacancy['employerManager']
    elif missing == 'manager_id':
        del vacancy['employerManager']['@managerId']
    elif missing == 'hhid':
        del vacancy['employerManager']['@hhid']
    elif missing == 'activity':
        payload['applicantEmployerManagersActivity'].pop()
    else:
        vacancy['employerManager']['@managerId'] = 88
    result = ns.parse_page(html(payload))
    assert result['ok']
    assert result['records'][0]['manager_activity'] is None


@pytest.mark.parametrize('value', ['PRIVATE NAME', {'@firstName': 'PRIVATE FIRST'}, True, None])
def test_manager_activity_durations_retain_only_numbers(value):
    payload = state()
    payload['applicantEmployerManagersActivity'][-1].update(
        inactive_days=value, **{'@inactiveMinutes': value})
    row = ns.parse_page(html(payload))['records'][0]
    assert row['manager_activity'] == {
        'inactive_days': None, 'inactiveMinutes': None, 'trl_code': 'online'}


@pytest.mark.parametrize('code,expected', [
    ('online', 'online'), ('offline', 'offline'), ('PRIVATE NAME', 'other'),
    ({'name': 'PRIVATE NAME'}, None), (True, None), (2, None), (None, None),
])
def test_manager_activity_status_in_jsonl(tmp_path, monkeypatch, code, expected):
    payload = state()
    payload['applicantEmployerManagersActivity'][-1]['trl_code'] = code
    collect, _, _ = transport(tmp_path, monkeypatch,
        lambda req: httpx.Response(200, text=html(payload)))
    assert collect()['complete']
    assert rows(tmp_path)[0]['manager_activity'] == {
        'inactive_days': 1, 'inactiveMinutes': 90, 'trl_code': expected}


@pytest.mark.parametrize('last_state', ['DISCARD', 'RESPONSE', 'INTERVIEW'])
def test_employer_status_allowlist(last_state):
    payload = state()
    payload['applicantNegotiations']['topicList'][0]['lastState'] = last_state
    row = ns.parse_page(html(payload))['records'][0]
    assert row['lastState'] == last_state
    assert row['lastEmployerState'] is None
    assert row['employerSubState'] == 'NEW'
    assert row['lastFunnelStage'] == 'SCREENING'


def test_parse_archived_and_company_name_fallback():
    payload = state(archived=True)
    del payload['vacanciesShort']['vacanciesList'][0]['company']['visibleName']
    row = ns.parse_page(html(payload), expected_filter='archived')['records'][0]
    assert row['employer_politeness'] is row['manager_activity'] is None
    assert row['viewedByOpponent'] is True
    assert row['company_name'] == 'Fallback company'


def test_vacancy_id_and_xml_attributes():
    payload = state()
    vacancy = payload['vacanciesShort']['vacanciesList'][0]
    vacancy['vacancyId'] = vacancy.pop('id')
    vacancy['@closedForApplicants'] = vacancy.pop('closedForApplicants')
    company = vacancy['company']
    company['@id'] = company.pop('id')
    row = ns.parse_page(html(payload))['records'][0]
    assert row['vacancy_name'] == 'Synthetic vacancy' and row['company_id'] == 8
    assert row['closed_for_applicants'] is False


@pytest.mark.parametrize('markup,reason', [
    ('<html>Anonymous</html>', 'template_missing'), (html({}), 'negotiations_missing'),
    ('<template id="HH-Lux-InitialState">{bad}</template>', 'invalid_json'),
    ('<template id="HH-Lux-InitialState">{}', 'template_unclosed'),
    (html({'applicantNegotiations': []}), 'negotiations_missing'),
    (html([]), 'negotiations_missing'),
    (html(dict(state(), userType='anonymous')), 'anonymous_page'),
    (html(dict(state(), luxPageName='ForbiddenPage')), 'anonymous_page'),
])
def test_parse_failure_is_explicit(markup, reason):
    assert ns.parse_page(markup) == {'ok': False, 'reason': reason}


def test_jsonl_allowlist_and_append_only_without_state_writes(tmp_path, monkeypatch):
    # Keep the collector's file inventory separate from harness-owned directories.
    home = tmp_path / 'collector'
    home.mkdir()
    collect, calls, pauses = transport(home, monkeypatch, lambda req: httpx.Response(200, text=html(state())))
    guard = home / 'hh_guard_state.json'
    guard.write_text(json.dumps({'blocked_until': ''}))
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in home.iterdir()}
    assert collect()['complete']
    previous = (home / 'negotiation_snapshots.jsonl').read_bytes()
    assert collect()['complete']
    assert (home / 'negotiation_snapshots.jsonl').read_bytes().startswith(previous)
    assert len(calls) == 2 and not pauses
    row, summary = rows(home)[:2]
    assert row['chatId'] == 5700000002
    assert type(row['chatId']) is int
    assert set(row) == set(ns.TOPIC_FIELDS + ns.VACANCY_FIELDS) | {
        'snapshot_id', 'fetched_at', 'filter', 'page', 'employer_politeness', 'manager_activity'}
    assert set(row['employer_politeness']) == set(ns.POLITENESS_FIELDS)
    assert set(row['manager_activity']) == set(ns.MANAGER_FIELDS)
    assert set(summary) == {'kind', 'snapshot_id', 'fetched_at', 'filter', 'counters', 'total',
                            'pages_read', 'complete', 'stop_reason'}
    assert 'PRIVATE' not in json.dumps(rows(home))
    assert '@firstName' not in previous.decode() and '@lastName' not in previous.decode()
    assert row['employerSubState'] == 'NEW' and row['lastFunnelStage'] == 'SCREENING'
    assert row['vacancy_name'] == 'Synthetic vacancy' and row['company_name'] == 'Synthetic company'
    assert summary['counters'] == {'total': {'all': 1, 'archived': 0}, 'new': {'all': 0}}
    assert summary['snapshot_id'] == row['snapshot_id'] and summary['stop_reason'] is None
    assert (home / 'negotiation_snapshots.jsonl').stat().st_mode & 0o777 == 0o600
    assert set(p.name for p in home.iterdir()) == set(before) | {'negotiation_snapshots.jsonl'}
    for name, values in before.items():
        p = home / name
        assert (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) == values


@pytest.mark.parametrize('pages,archived', [(7, False), (88, True)])
def test_zero_based_one_pass_and_pacing(tmp_path, monkeypatch, pages, archived):
    def handler(req):
        page = int(req.url.params['page'])
        assert req.url.params.get('filter', 'active') == ('archived' if archived else 'active')
        return httpx.Response(200, text=html(state(page, pages, archived=archived)))
    collect, calls, pauses = transport(tmp_path, monkeypatch, handler)
    result = collect(archived=archived)
    assert result['complete'] and result['pages_read'] == pages
    assert [int(req.url.params['page']) for req in calls] == list(range(pages))
    assert pauses == [6] * (pages - 1)


@pytest.mark.parametrize('limit,read,complete', [(1, 1, False), (2, 2, False), (7, 7, True), (10, 7, True)])
def test_max_pages(tmp_path, monkeypatch, limit, read, complete):
    collect, calls, pauses = transport(tmp_path, monkeypatch,
        lambda req: httpx.Response(200, text=html(state(int(req.url.params['page']), 7))))
    result = collect(max_pages=limit)
    assert result['complete'] is complete and result['pages_read'] == len(calls) == read
    assert result['stop_reason'] == (None if complete else 'max_pages')
    assert len(pauses) == read - 1


@pytest.mark.parametrize('failure,reason', [
    ('403', 'http_403'), ('500', 'http_500'), ('login', 'login_redirect'),
    ('anonymous', 'anonymous_page'), ('foreign', 'unexpected_redirect'),
    ('captcha', 'antibot_captcha'), ('ddos', 'antibot_ddos_guard'),
    ('missing', 'negotiations_missing'), ('template', 'template_missing'),
    ('revision', 'cookies_changed'), ('timeout', 'collection_error'),
    ('duplicate', 'duplicate_topics'), ('paging', 'page_mismatch'),
])
def test_stop_on_second_page_no_followups(tmp_path, monkeypatch, failure, reason):
    def handler(req):
        page = int(req.url.params['page'])
        if page == 0:
            return httpx.Response(200, text=html(state(0, 7)))
        if failure.isdigit():
            return httpx.Response(int(failure))
        if failure in {'login', 'anonymous', 'foreign'}:
            return httpx.Response(302, headers={'location': {
                'login': '/account/login?token=PRIVATE', 'anonymous': '/',
                'foreign': 'https://example.invalid/applicant/negotiations'}[failure]})
        if failure == 'captcha':
            return httpx.Response(200, text='<form id="captcha">Подтвердите, что вы не робот</form>' + html(state(1, 7)))
        if failure == 'ddos':
            return httpx.Response(200, text='DDOS-GUARD')
        if failure == 'revision':
            (tmp_path / 'hh_cookies.json').write_text('[]')
        if failure == 'timeout':
            raise httpx.ReadTimeout('PRIVATE URL TOKEN', request=req)
        body = html(state(0 if failure in {'duplicate', 'paging'} else page, 7))
        if failure == 'duplicate':
            payload = state(1, 7)
            payload['applicantNegotiations']['topicList'][0]['id'] = 100
            body = html(payload)
        if failure == 'missing':
            body = html({})
        if failure == 'template':
            body = '<html>Anonymous</html>'
        return httpx.Response(200, text=body)
    collect, calls, pauses = transport(tmp_path, monkeypatch, handler)
    result = collect()
    assert result['stop_reason'] == reason and result['complete'] is False
    assert result['pages_read'] == 1 and len(calls) == 2 and pauses == [6]
    assert len(rows(tmp_path)) == 2 and 'PRIVATE' not in json.dumps(rows(tmp_path))


@pytest.mark.parametrize('guard,reason', [
    ({'blocked_until': (datetime.now().astimezone() + timedelta(days=1)).isoformat()}, 'guard_blocked'),
    ({'blocked_until': 'broken'}, 'guard_invalid'), ('broken json', 'guard_unavailable'),
])
def test_guard_blocks_without_start_or_writes(tmp_path, monkeypatch, guard, reason):
    collect, calls, pauses = transport(tmp_path, monkeypatch, lambda req: pytest.fail('request while blocked'))
    path = tmp_path / 'hh_guard_state.json'
    path.write_text(guard if isinstance(guard, str) else json.dumps(guard))
    before = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(ns, '_authenticated_client', lambda *args, **kwargs: pytest.fail('client while blocked'))
    result = collect()
    assert result['stop_reason'] == reason and not result['complete'] and result['pages_read'] == 0
    assert not calls and not pauses and len(rows(tmp_path)) == 1
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_revision_changes_during_sleep_stop_before_next_get(tmp_path, monkeypatch):
    collect, calls, _ = transport(tmp_path, monkeypatch, lambda req: httpx.Response(200, text=html(state(0, 7))))
    def sleep(_):
        (tmp_path / 'hh_cookies.json').write_text('[]')
    result = ns.collect(home_dir=tmp_path, cookies_file=tmp_path / 'hh_cookies.json', sleep=sleep)
    assert result['stop_reason'] == 'cookies_changed' and len(calls) == 1


def test_guard_becomes_blocked_during_sleep(tmp_path, monkeypatch):
    collect, calls, _ = transport(tmp_path, monkeypatch, lambda req: httpx.Response(200, text=html(state(0, 7))))
    def sleep(_):
        (tmp_path / 'hh_guard_state.json').write_text(json.dumps({
            'blocked_until': (datetime.now().astimezone() + timedelta(days=1)).isoformat()}))
    result = ns.collect(home_dir=tmp_path, cookies_file=tmp_path / 'hh_cookies.json', sleep=sleep)
    assert result['stop_reason'] == 'guard_blocked' and len(calls) == 1
    assert result['pages_read'] == 1 and not result['complete']


def test_expired_guard_is_not_cleared(tmp_path, monkeypatch):
    collect, calls, _ = transport(tmp_path, monkeypatch, lambda req: httpx.Response(200, text=html(state())))
    path = tmp_path / 'hh_guard_state.json'
    path.write_text(json.dumps({'blocked_until': (datetime.now().astimezone() - timedelta(days=1)).isoformat()}))
    original = path.read_bytes(), path.stat().st_mtime_ns
    assert collect()['complete'] and len(calls) == 1
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original


def test_regional_redirect_is_get_and_login_is_not_fetched(tmp_path, monkeypatch):
    def handler(req):
        if req.url.host == 'hh.ru':
            return httpx.Response(302, headers={'location': 'https://spb.hh.ru' + req.url.raw_path.decode()})
        return httpx.Response(200, text=html(state()))
    collect, calls, pauses = transport(tmp_path, monkeypatch, handler)
    assert collect()['complete']
    assert [req.url.host for req in calls] == ['hh.ru', 'spb.hh.ru'] and not pauses


def test_region_is_reused_for_later_pages(tmp_path, monkeypatch):
    def handler(req):
        if req.url.host == 'hh.ru':
            return httpx.Response(302, headers={'location': 'https://spb.hh.ru' + req.url.raw_path.decode()})
        return httpx.Response(200, text=html(state(int(req.url.params['page']), 3)))
    collect, calls, pauses = transport(tmp_path, monkeypatch, handler)
    assert collect()['complete']
    assert [req.url.host for req in calls] == ['hh.ru'] + ['spb.hh.ru'] * 3
    assert pauses == [6, 6]


def test_empty_account_and_missing_cookies(tmp_path, monkeypatch):
    payload = state()
    payload['applicantNegotiations'].update(total=0, pageCount=0, topicList=[])
    collect, calls, _ = transport(tmp_path, monkeypatch, lambda req: httpx.Response(200, text=html(payload)))
    assert collect()['complete'] and len(calls) == 1
    (tmp_path / 'hh_cookies.json').unlink()
    result = collect()
    assert result['stop_reason'] == 'cookies_missing' and len(calls) == 1


def test_cli_loads_profile_without_activation(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    import profile as profile_mod
    monkeypatch.setattr(profile_mod, 'load_profile', lambda name: SimpleNamespace(
        home_dir=tmp_path, hh=SimpleNamespace(cookies_file=tmp_path / 'cookies.json')) if name == 'synthetic' else None)
    monkeypatch.setattr(profile_mod, 'activate', lambda *args: pytest.fail('activation'))
    seen = []
    monkeypatch.setattr(ns, 'collect', lambda **kwargs: seen.append(kwargs) or {'complete': False})
    assert ns.main(['--profile', 'synthetic', '--archived', '--max-pages', '2']) == 1
    assert seen == [dict(home_dir=tmp_path, cookies_file=tmp_path / 'cookies.json', archived=True, max_pages=2)]
    assert json.loads(capsys.readouterr().out) == {'complete': False}
    with pytest.raises(SystemExit):
        ns.main(['--max-pages', '0'])


def test_run_sh_forwards_snapshot_options(tmp_path):
    executable = tmp_path / 'python'
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o755)
    env = dict(os.environ, JOB_HUNTER_PYTHON=str(executable), JOB_HUNTER_ENV_FILE=str(tmp_path / 'missing'))
    result = subprocess.run([str(Path(__file__).resolve().parents[1] / 'run.sh'), '--profile',
                             'synthetic', 'negotiations-snapshot', '--archived', '--max-pages', '2'],
                            env=env, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ['-m', 'hh.negotiation_snapshot', '--profile', 'synthetic',
                                         '--archived', '--max-pages', '2']
