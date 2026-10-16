"""Response routing aliases share one spelling across guards and observation."""
import asyncio
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from hh.recovery import _application_request, _unknown_classification
from hh.response_path import CANONICAL_PATH_SCRIPT, RESPONSE_PATH, canonical_path
from hh.submit_boundary import popup_request_matches
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from tests.test_hh_react_response_state_helpers import POPUP_FIELDS, POPUP_URL, _request
from tests.test_hh_recovery_attempt_window import URL, monitored_client, request


ALIASES = [
    '/applicant/%76acancy_response/popup',
    '/applicant/%2576acancy_response/popup',
    '/applicant/' + '%' + '25' * 40 + '76acancy_response/popup',
    '/APPLICANT/VACANCY_RESPONSE/POPUP',
    '//applicant///vacancy_response//popup//',
    '/applicant/./vacancy_response/./popup/',
    '/unused/../applicant/vacancy_response/other/../popup',
    '/../../applicant/vacancy_response/popup',
    '/applicant/%2e/vacancy_response/%2e%2e/vacancy_response/popup',
    '/applicant./vacancy_response.../popup..',
    '/applicant /vacancy_response/ popup/ \t\r\n',
    '/applicant/%2076acancy_response/popup'.replace('%2076', '%25%20%37%36'),
    '/applicant/%c2%a0vacancy_response/popup',
    '/applicant%2fvacancy_response%252fpopup',
    r'/applicant\vacancy_response\popup',
    '/applicant%5cvacancy_response%255cpopup',
]
NON_ALIASES = [
    '/applicant/vacancy/response/popup',
    '/applicant/vacancy_response/../profile',
    '/applicant/vacancy_response/popup/extra',
    '/applicant/vacancy_response;parameter/popup',
    '/applicant/vacancy+response/popup',
    '/applicant/vacancy%3f_response/popup',
    '/applicant/vacancy%23_response/popup',
    '/applicant/vacancy%00_response/popup',
    '/applicant/vacancy%zz_response/popup',
    '/applicant/vacancy＿response/popup',
]


class Unreadable(SimpleNamespace):
    @property
    def post_data(self):
        raise UnicodeDecodeError('utf8', b'\xff', 0, 1, 'offline binary body')


@pytest.mark.parametrize('path', ALIASES)
def test_aliases_classify_count_stop_and_record_status_consistently(tmp_path, path):
    assert canonical_path(path) == RESPONSE_PATH + '/popup'
    assert canonical_path(canonical_path(path)) == canonical_path(path)
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = Unreadable(method='POST', url='https://hh.ru' + path, headers={}, frame=SimpleNamespace(page=page))
    # Use the same path with ordinary bodies too, so the old opacity fallback
    # cannot make a broken route recognizer look correct.
    for body in (None, 'unrelated=value'):
        readable = request(page, event.url)
        readable.post_data = body
        assert _unknown_classification(readable, 'test', None, None)['application_reason'] == 'response_path'
    assert _application_request(event)

    async def operation():
        attempt = client._external_attempt
        attempt.begin()
        attempt._hh_application_posts.append({'request': object(), 'owned': True, 'status': 200})
        context._hh_action_watch.application_post_sequence = 1
        context.emit('request', event)
        assert context._hh_action_watch.application_post_sequence == 2
        assert len(attempt._hh_application_posts) == 2
        diagnostic = attempt._hh_application_posts[1]['classification']
        assert diagnostic['path'] == RESPONSE_PATH + '/popup'
        assert diagnostic['path_category'] == 'response'
        assert diagnostic['application_reason'] == 'response_path'
        assert diagnostic['body_state'] == 'unreadable'
        assert monitor.unknown and context._hh_action_watch.unknown and attempt.uncertain
        context.emit('response', SimpleNamespace(url=event.url, request=event, status=200))
        assert attempt._hh_application_posts[1]['status'] == 200
        return {'ok': True}

    assert asyncio.run(run_native_attempt(client, repository, URL, operation))['uncertain']


@pytest.mark.parametrize('path', ALIASES)
def test_popup_payload_approval_uses_same_canonical_path(path):
    assert popup_request_matches({'url': POPUP_URL, 'fields': POPUP_FIELDS},
                                 _request(url='https://hh.ru' + path))


@pytest.mark.parametrize('path', NON_ALIASES)
def test_distinct_paths_do_not_gain_popup_payload_approval(path):
    assert canonical_path(path) != RESPONSE_PATH + '/popup'
    assert not popup_request_matches({'url': POPUP_URL, 'fields': POPUP_FIELDS},
                                     _request(url='https://hh.ru' + path))


def test_renderer_and_python_normalize_identically_and_encoded_binary_post_blocks(tmp_path):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=[
                '--disable-background-networking',
                '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'])
            try:
                context = await browser.new_context(service_workers='block')
                async def offline(route):
                    await route.fulfill(content_type='text/html', body='<body>Offline</body>')
                await context.route('**/*', offline)
                page = await context.new_page()
                await page.goto('https://hh.ru/offline')
                cases = ALIASES + NON_ALIASES + [RESPONSE_PATH, '/analytics/pageview',
                    '/applicant/vacancy_response/popup%zz', '/%EF%BB%BFapplicant/vacancy_response']
                actual = await page.evaluate('(paths) => paths.map(' + CANONICAL_PATH_SCRIPT + ')', cases)
                assert actual == [canonical_path(path) for path in cases]
                from hh.recovery import monitor_page
                client = SimpleNamespace(_page=page, _context=context)
                monitor = monitor_page(client, page)
                repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')

                async def operation():
                    attempt = client._external_attempt
                    attempt.begin()
                    attempt._hh_application_posts.append({'request': object(), 'owned': True, 'status': 200})
                    context._hh_action_watch.application_post_sequence = 1
                    await page.evaluate("""async () => {
                        await fetch('/applicant/%76acancy_response/popup', {
                            method:'POST', body:new Uint8Array([255,0,254])});
                    }""")
                    posts = attempt._hh_application_posts
                    assert len(posts) == 2
                    diagnostic = posts[1]['classification']
                    assert diagnostic['body_state'] == 'unreadable'
                    assert diagnostic['path_category'] == 'response'
                    assert diagnostic['application_reason'] == 'response_path'
                    assert monitor.unknown and context._hh_action_watch.unknown
                    return {'ok': True}

                assert (await run_native_attempt(client, repository, URL, operation))['uncertain']
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('path', [RESPONSE_PATH, '/applicant/%76acancy_response/popup'])
def test_unreadable_response_body_on_read_method_still_counts_and_stops(tmp_path, method, path):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = Unreadable(method=method, url='https://hh.ru' + path, headers={},
                       frame=SimpleNamespace(page=page))

    async def operation():
        attempt = client._external_attempt
        attempt.begin()
        attempt._hh_application_posts.append({'request': object(), 'owned': True, 'status': 200})
        context._hh_action_watch.application_post_sequence = 1
        context.emit('request', event)
        assert context._hh_action_watch.application_post_sequence == 2
        assert len(attempt._hh_application_posts) == 1
        assert monitor.unknown and context._hh_action_watch.unknown
        assert monitor.last_unknown_diagnostic['application_reason'] == 'unreadable_body'
        return {'ok': True}

    assert asyncio.run(run_native_attempt(client, repository, URL, operation))['uncertain']
