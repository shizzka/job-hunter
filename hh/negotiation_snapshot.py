"""One-pass, HTTP-only HH negotiations observations; no session/state writes."""
from __future__ import annotations

import argparse
import json
import random
import time
import uuid
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from hh.collection_journal import append_records
from hh.observation import initial_state, _number
from hh_guard import _parse_datetime, _valid_state, detect_antibot_kind
from hh_response_counter import _authenticated_client
from state_store.hh_cookies import HHCookieRepository


TOPIC_FIELDS = (
    'id', 'vacancyId', 'employerId', 'employerManagerId', 'resumeId',
    'creationTimeMillis', 'lastModifiedMillis', 'employerLastChangeTime',
    'viewedByOpponent', 'lastEmployerState', 'lastState', 'lastSubState',
    'employerSubState', 'lastFunnelStage',
    'currentTopicType', 'initialTopicType', 'archived', 'hasNewMessages',
    'conversationMessagesCount', 'conversationUnreadByEmployerCount',
    'hasResponseLetter', 'declinedByApplicant', 'topicSource', 'platform',
)
POLITENESS_FIELDS = ('allTopicCount', 'readTopicPercent', 'replyTotalWorkingTimeDays')
MANAGER_FIELDS = ('inactive_days', 'inactiveMinutes', 'trl_code')
VACANCY_FIELDS = ('vacancy_name', 'company_id', 'company_name', 'company_trusted',
                  'company_accredited_it', 'vacancy_creation_time',
                  'vacancy_publication_time', 'closed_for_applicants')


def _object(value):
    return value if isinstance(value, dict) else {}


def _scalar(value):
    # Do not copy nested objects, even under an allowed field name.
    return value if type(value) in (str, int, float, bool) else None


def _pick(value, fields):
    value = _object(value)
    return {key: _scalar(value.get(key)) for key in fields}


def parse_page(html, *, expected_filter='active'):
    """Pure HTML -> allowlisted records, or an explicit reason; never raises."""
    try:
        try:
            state = initial_state(html)
        except ValueError as exc:
            return {'ok': False, 'reason': str(exc)}
        state = _object(state)
        if (str(state.get('userType', '')).casefold() == 'anonymous'
                or str(state.get('luxPageName', '')).casefold() == 'forbiddenpage'):
            return {'ok': False, 'reason': 'anonymous_page'}
        model = state.get('applicantNegotiations')
        if not isinstance(model, dict):
            return {'ok': False, 'reason': 'negotiations_missing'}
        if model.get('filterInUse', expected_filter) != expected_filter:
            return {'ok': False, 'reason': 'filter_mismatch'}
        total, page_count = _number(model.get('total')), _number(model.get('pageCount'))
        topics = model.get('topicList')
        if (total is None or page_count is None or not isinstance(topics, list)
                or (total > 0 and page_count == 0)):
            return {'ok': False, 'reason': 'invalid_negotiations'}
        politeness = _object(_object(state.get('applicantEmployerPoliteness')).get('employerPolitenessIndexes'))
        managers = {str(item.get('@managerHhid')): item
                    for item in state.get('applicantEmployerManagersActivity', []) or []
                    if isinstance(item, dict)}
        vacancies = {str(item.get('vacancyId', item.get('id', item.get('@id')))): item
                     for item in _object(state.get('vacanciesShort')).get('vacanciesList', []) or []
                     if isinstance(item, dict)}
        # Topic manager IDs and activity HHIDs belong to different namespaces.
        manager_hhids = {}
        for vacancy in vacancies.values():
            manager = _object(vacancy.get('employerManager'))
            manager_id, hhid = manager.get('@managerId'), manager.get('@hhid')
            if manager_id is not None and hhid is not None:
                manager_hhids[str(manager_id)] = str(hhid)
        records = []
        for topic in topics:
            if not isinstance(topic, dict) or _number(topic.get('id')) is None:
                return {'ok': False, 'reason': 'invalid_topic'}
            row = _pick(topic, TOPIC_FIELDS)
            employer = politeness.get(str(topic.get('employerId')))
            manager = managers.get(manager_hhids.get(str(topic.get('employerManagerId'))))
            row['employer_politeness'] = _pick(employer, POLITENESS_FIELDS) if isinstance(employer, dict) else None
            row['manager_activity'] = ({
                'inactive_days': _number(manager.get('inactive_days')),
                'inactiveMinutes': _number(manager.get('@inactiveMinutes')),
                'trl_code': _number(manager.get('trl_code')),
            } if manager is not None else None)
            vacancy = _object(vacancies.get(str(topic.get('vacancyId'))))
            company = _object(vacancy.get('company'))
            row.update({
                'vacancy_name': _scalar(vacancy.get('name')),
                'company_id': _number(company.get('id', company.get('@id'))),
                'company_name': _scalar(company.get('visibleName') or company.get('name')),
                'company_trusted': _scalar(company.get('@trusted')),
                'company_accredited_it': _scalar(company.get('accreditedITEmployer')),
                'vacancy_creation_time': _scalar(vacancy.get('creationTime')),
                'vacancy_publication_time': _scalar(vacancy.get('publicationTime')),
                'closed_for_applicants': _scalar(vacancy.get('closedForApplicants', vacancy.get('@closedForApplicants'))),
            })
            records.append(row)
        # Counter keys come from HH tabs; retain numeric counts only, never text.
        counters = {group: {key: _number(value) for key, value in _object(
            _object(state.get('applicantNegotiationsCounters')).get(group)).items()
            if key in {'all', 'active', 'archived', 'deleted', 'invitation', 'response', 'discard'}}
            for group in ('total', 'new')}
        paging = _object(model.get('paging'))
        return {'ok': True, 'records': records, 'total': total, 'page_count': page_count,
                'counters': counters, 'page': _number(paging.get('page'))}
    except Exception:
        return {'ok': False, 'reason': 'invalid_state'}


def guard_stop_reason(home_dir):
    """Same guard timestamp/schema as hh_guard, with strictly read-only loading."""
    try:
        with (Path(home_dir) / 'hh_guard_state.json').open(encoding='utf-8') as stream:
            state = json.load(stream)
    except FileNotFoundError:
        return None
    except Exception:
        return 'guard_unavailable'
    if not isinstance(state, dict) or not _valid_state(state):
        return 'guard_invalid'
    until = _parse_datetime(state.get('blocked_until', ''))
    return 'guard_blocked' if until and until > datetime.now().astimezone() else None


class _ChallengeText(HTMLParser):
    """Ignore normal captcha configuration in JSON/scripts, retain visible UI."""
    def __init__(self):
        super().__init__()
        self.hidden = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {'template', 'script', 'style', 'noscript'}:
            self.hidden.append(tag)
        if not self.hidden:
            self.parts.extend(value or '' for key, value in attrs
                              if key in {'id', 'class', 'data-qa', 'action'})

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class _Stop(Exception):
    pass


def _target_reason(url, page, filter_name):
    target = urlsplit(url)
    if target.path.startswith(('/account/', '/auth/', '/login', '/signup')):
        return 'login_redirect'
    if (target.scheme != 'https' or not target.hostname
            or not (target.hostname == 'hh.ru' or target.hostname.endswith('.hh.ru'))
            or target.username or target.password or target.port not in (None, 443)):
        return 'unexpected_redirect'
    if target.path.rstrip('/') != '/applicant/negotiations':
        return 'anonymous_page'
    query = parse_qs(target.query)
    if (set(query) - {'page', 'filter'} or query.get('page', ['0']) != [str(page)]
            or query.get('filter', ['active']) != [filter_name]):
        return 'unexpected_paging'
    return None


def collect(*, home_dir, cookies_file, archived=False, max_pages=None,
            sleep=time.sleep, uniform=random.uniform):
    """Append one snapshot; every failure stops the pass without a retry."""
    if max_pages is not None and (type(max_pages) is not int or max_pages < 1):
        raise ValueError('max_pages must be positive')
    filter_name = 'archived' if archived else 'active'
    summary = {'kind': 'snapshot_summary', 'snapshot_id': uuid.uuid4().hex,
               'fetched_at': datetime.now().astimezone().isoformat(timespec='microseconds'),
               'filter': filter_name, 'counters': None, 'total': None,
               'pages_read': 0, 'complete': False, 'stop_reason': None}
    path = Path(home_dir) / 'negotiation_snapshots.jsonl'
    repository = HHCookieRepository(cookies_file)

    def cookies_snapshot():
        # snapshot() creates/chmods a cookie lock sidecar. This reader must not
        # touch session state: use the same validated, inode/hash revision read.
        return repository._snapshot_unlocked()

    def fresh():
        reason = guard_stop_reason(home_dir)
        if reason:
            raise _Stop(reason)
        try:
            current_revision = cookies_snapshot()[1]
        except Exception:
            raise _Stop('cookies_changed') from None
        if current_revision != revision:
            raise _Stop('cookies_changed')

    try:
        reason = guard_stop_reason(home_dir)
        if reason:
            raise _Stop(reason)
        cookies, revision = cookies_snapshot()
        if not cookies:
            raise _Stop('cookies_missing')
        with _authenticated_client(str(cookies_file), cookies=cookies) as client:
            page, expected_pages, seen = 0, None, set()
            origin = 'https://hh.ru'
            while True:
                fresh()
                if page:
                    sleep(uniform(4, 8))
                    fresh()
                query = {'page': page}
                if archived:
                    query['filter'] = 'archived'
                url = origin + '/applicant/negotiations?' + urlencode(query)
                visited = set()
                # Follow only validated regional GET redirects. Never fetch login.
                for _ in range(4):
                    reason = _target_reason(url, page, filter_name)
                    if reason:
                        raise _Stop(reason)
                    if url in visited:
                        raise _Stop('redirect_loop')
                    visited.add(url)
                    fresh()
                    response = client.get(url, follow_redirects=False)
                    fresh()
                    reason = _target_reason(str(response.url), page, filter_name)
                    if reason:
                        raise _Stop(reason)
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get('location')
                        if not location:
                            raise _Stop('redirect_missing_location')
                        url = urljoin(str(response.url), location)
                        continue
                    if response.status_code != 200:
                        raise _Stop(f'http_{response.status_code}')
                    break
                else:
                    raise _Stop('redirect_limit')
                parser = _ChallengeText()
                parser.feed(response.text)
                antibot = detect_antibot_kind(' '.join(parser.parts))
                if antibot:
                    raise _Stop('antibot_' + antibot)
                parsed = parse_page(response.text, expected_filter=filter_name)
                if not parsed['ok']:
                    raise _Stop(parsed['reason'])
                # Reuse the verified region instead of repeating a redirect on
                # every page of the same snapshot.
                origin = 'https://' + urlsplit(str(response.url)).netloc
                if parsed['page'] is not None and parsed['page'] != page:
                    raise _Stop('page_mismatch')
                if expected_pages is None:
                    expected_pages = max(1, parsed['page_count'])
                    summary.update(total=parsed['total'], counters=parsed['counters'])
                elif parsed['page_count'] != expected_pages or parsed['total'] != summary['total']:
                    raise _Stop('pagination_changed')
                ids = [str(row['id']) for row in parsed['records']]
                if len(set(ids)) != len(ids) or seen.intersection(ids):
                    raise _Stop('duplicate_topics')
                fresh()
                fetched_at = datetime.now().astimezone().isoformat(timespec='microseconds')
                append_records(path, [dict(snapshot_id=summary['snapshot_id'], fetched_at=fetched_at,
                                           filter=filter_name, page=page, **row) for row in parsed['records']])
                seen.update(ids)
                summary['pages_read'] += 1
                page += 1
                if page >= expected_pages:
                    fresh()
                    if len(seen) != summary['total']:
                        raise _Stop('topic_count_mismatch')
                    summary['complete'] = True
                    break
                if max_pages is not None and page >= max_pages:
                    raise _Stop('max_pages')
    except _Stop as exc:
        summary['complete'] = False
        summary['stop_reason'] = str(exc)
    except Exception:
        # Never log transport exception text (URLs/headers can contain secrets).
        summary['complete'] = False
        summary['stop_reason'] = 'collection_error'
    append_records(path, [summary])
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only HH negotiation snapshot')
    parser.add_argument('--profile', default='default')
    parser.add_argument('--archived', action='store_true')
    parser.add_argument('--max-pages', type=int)
    args = parser.parse_args(argv)
    if args.max_pages is not None and args.max_pages < 1:
        parser.error('--max-pages must be positive')
    import profile as profile_mod
    profile = profile_mod.load_profile(args.profile)  # no activation, locks, logging or state writes
    summary = collect(home_dir=profile.home_dir, cookies_file=profile.hh.cookies_file,
                      archived=args.archived, max_pages=args.max_pages)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
