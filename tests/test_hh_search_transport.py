"""Read-only search request validation, no live browser/network."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hh.search import HHSearchAuthError, HHSearchStopped, read_search_html

URL='https://hh.ru/search/vacancy?text=synthetic'


def response(url=URL, *, status=200, body='synthetic', **headers):
    return SimpleNamespace(status=status,url=url,
        headers={'content-type':'text/html',**headers},
        text=AsyncMock(return_value=body),dispose=AsyncMock())


def session(responses):
    get=AsyncMock(side_effect=responses)
    client=SimpleNamespace(_context=SimpleNamespace(request=SimpleNamespace(get=get)),
        _page=object(),_ensure_expected_ui=AsyncMock())
    return client,get


def test_only_same_query_trusted_regional_redirects_are_followed():
    target='https://spb.hh.ru/search/vacancy?text=synthetic'
    redirects=[response(status=302,location=target),response(target)]
    client,get=session(redirects)
    assert asyncio.run(read_search_html(client,URL))==('synthetic',target)
    assert get.await_count==2
    assert get.await_args_list[1].args==(target,)
    assert all(c.kwargs=={'max_redirects':0,'timeout':30000} for c in get.await_args_list)
    assert all(r.dispose.await_count==1 for r in redirects)


@pytest.mark.parametrize('target',[
    'https://evil.test/search/vacancy?text=synthetic',
    'https://hh.ru/account/login?text=synthetic',
    'https://hh.ru/search/vacancy?text=changed',
    'https://hh.ru/search/vacancy?text=synthetic&resumeId=unapproved',
    'https://user@hh.ru/search/vacancy?text=synthetic',
    'http://hh.ru/search/vacancy?text=synthetic',
])
def test_redirect_validation_stops_before_second_get(target):
    first=response(status=302,location=target)
    client,get=session([first])
    with pytest.raises(ValueError): asyncio.run(read_search_html(client,URL))
    assert get.await_count==1
    first.dispose.assert_awaited_once()


@pytest.mark.parametrize('case',[
    {'status':403}, {'status':429}, {'content-type':'application/json'},
    {'body':'<script>{"userType":"anonymous"}</script>'},
    {'body':'<script>{"luxPageName":"ForbiddenPage"}</script>'},
    {'status':302,'location':''},
    {'url':'https://evil.test/search/vacancy?text=synthetic'},
])
def test_unknown_response_auth_and_format_fail_closed(case):
    item=response(**case)
    client,get=session([item])
    with pytest.raises(ValueError): asyncio.run(read_search_html(client,URL))
    assert get.await_count==1
    item.dispose.assert_awaited_once()


def test_mutating_get_payload_rejected_before_network():
    client,get=session([])
    with pytest.raises(ValueError): asyncio.run(read_search_html(client,URL+'&resume_id=target'))
    get.assert_not_awaited()


def test_redirect_chain_is_bounded():
    items=[response(status=302,location=URL) for _ in range(4)]
    client,get=session(items)
    with pytest.raises(ValueError): asyncio.run(read_search_html(client,URL))
    assert get.await_count==4 and all(r.dispose.await_count==1 for r in items)


@pytest.mark.parametrize('case,error_type', [
    ({'status': 401}, HHSearchAuthError),
    ({'status': 403}, HHSearchStopped),
    ({'status': 429}, HHSearchStopped),
    ({'body': '<script>{"userType":"anonymous"}</script>'}, HHSearchAuthError),
    ({'body': '<script>{"luxPageName":"ForbiddenPage"}</script>'}, HHSearchAuthError),
    ({'status': 302, 'location': '/account/login'}, HHSearchAuthError),
    ({'status': 302, 'location': '/auth/login'}, HHSearchAuthError),
    ({'status': 302, 'location': '/captcha'}, HHSearchStopped),
    ({'status': 302, 'location': 'https://evil.test/account/login'}, HHSearchStopped),
    ({'url': 'https://evil.test/search/vacancy?text=synthetic'}, HHSearchStopped),
])
def test_access_and_untrusted_redirects_are_source_stops(case, error_type):
    item = response(**case)
    client, get = session([item])
    with pytest.raises(error_type):
        asyncio.run(read_search_html(client, URL))
    assert get.await_count == 1
    item.dispose.assert_awaited_once()
