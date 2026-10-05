"""Synthetic resume identities and submit guards; never contact HH."""
from tests.browser_action_fakes import fake_cdp_context
import asyncio
import json
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

import config
import hh_client
import apply_orchestrator
import hh_resume_pipeline as pipeline
from hh import apply as hh_apply


@pytest.mark.parametrize("fragment,target,expected", [
    ('<input type="radio" name="resume_id" value="wrong" checked>'
     '<input type="radio" name="resume_id" value="target">', 'target', False),
    ('<input type="hidden" name="resume_id" value="target">', 'target', True),
    ('<input type="hidden" name="resume_id" value="target">'
     '<input type="radio" name="employer-question" value="yes" checked>', 'target', True),
    ('<input type="radio" name="employer-question" value="target" checked>', 'target', False),
    ('<input type="hidden" name="resume_id" value="wrong">'
     '<div data-qa="resume-title"><a href="/resume/target">QA</a></div>', 'target', False),
    ('<input type="hidden" name="resume_id" value="wrong" disabled>'
     '<input type="hidden" name="resumeId" value="target">', 'target', True),
    ('<input type="hidden" name="resume_id" value="target">'
     '<input type="hidden" name="resumeHash" value="wrong">', 'target', False),
    ('<div data-qa="resume-title">QA</div>', 'target', False),
    ('<input type="radio" name="resume_id" value="wrong" checked>'
     '<div role="listbox"><div data-qa="resume-title">'
     '<a href="/resume/target">QA</a></div></div>', 'target', False),
])
def test_selected_identity_is_unambiguous_and_not_an_unchecked_option(fragment, target, expected):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route("**/*", lambda route: route.abort())
                page = await context.new_page()
                await page.set_content('<form name="vacancy_response">' + fragment + '</form>')
                assert await hh_apply.selected_resume_matches(page, target, 'QA') is expected
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize("html", [
    '<form name="vacancy_response"><input name="resume_id" value="target"></form>'
    '<form name="vacancy_response"><input name="resume_id" value="wrong"></form>',
    '<div role="dialog"><input name="resume_id" value="target">Profile question</div>',
    '<form name="vacancy_response" style="display:none"><input name="resume_id" value="target"></form>'
    '<form name="vacancy_response"><input name="resume_id" value="wrong"></form>',
    '<form name="vacancy_response"><div data-qa="resume-title">QA</div></form>'
    '<div role="listbox"><label data-magritte-select-option="target" aria-selected="true">'
    '<input type="radio" name="a" value="target" checked><span data-qa="resume-title">QA</span></label>'
    '<label data-magritte-select-option="wrong" aria-selected="true"><input type="radio" name="b" value="wrong" checked>'
    '<span data-qa="resume-title">QA</span></label></div>',
])
def test_ambiguous_or_unrelated_response_surfaces_do_not_verify(html):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route("**/*", lambda route: route.abort())
                page = await context.new_page()
                await page.set_content(html)
                assert not await hh_apply.selected_resume_matches(page, 'target', 'QA')
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.fixture
def resume_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(tmp_path))
    monkeypatch.setattr(config, 'HH_RESUME_PIPELINE_FILE', str(tmp_path / 'pipeline.json'))
    monkeypatch.setattr(config, 'HH_RESUME_PIPELINE_ENABLED', False)
    monkeypatch.setattr(config, 'HH_APPLY_TRACE_ENABLED', False)
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', 'target')
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_TITLE', 'QA')
    for key in ('HH_SECONDARY_RESUME_ID', 'HH_SECONDARY_RESUME_TITLE', 'HH_TERTIARY_RESUME_ID', 'HH_TERTIARY_RESUME_TITLE'):
        monkeypatch.setattr(config, key, '')
    monkeypatch.setattr(apply_orchestrator.company_blacklist, 'is_blocked', lambda name: False)
    return tmp_path


class FakeClient:
    def __init__(self, resumes=None):
        self.resumes = resumes or []
        self.calls = []

    async def get_resume_ids(self):
        return self.resumes

    async def apply_to_vacancy(self, url, letter, **kwargs):
        self.calls.append(kwargs)
        return {'ok': True, 'resume_selection_verified': True, 'selected_resume_id': kwargs['preferred_resume_id']}


def vacancy():
    return {'id': 'hh:1', 'source': 'hh', 'url': 'https://hh.ru/vacancy/1'}


def test_pipeline_off_dispatch_always_passes_primary_id(resume_config):
    client = FakeClient([{'id': 'wrong', 'title': 'Other profession'}, {'id': 'target', 'title': 'QA'}])
    result = asyncio.run(apply_orchestrator.dispatch_apply(vacancy(), 'letter', hh_client=client))
    assert result['ok']
    assert client.calls[0]['preferred_resume_id'] == 'target'


@pytest.mark.parametrize("resumes", [[], [{'id': 'wrong', 'title': 'Other profession'}],
    [{'id': 'a', 'title': 'QA'}, {'id': 'b', 'title': 'QA'}], [{'id': 'a', 'title': 'QA engineer extra'}]])
def test_unresolved_or_ambiguous_title_blocks_dispatch(resume_config, monkeypatch, resumes):
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', '')
    client = FakeClient(resumes)
    result = asyncio.run(apply_orchestrator.dispatch_apply(vacancy(), 'letter', hh_client=client))
    assert not result['ok']
    assert result['reason'] == 'hh_resume_target_unresolved'
    assert not client.calls


def test_unique_configured_title_is_resolved_to_exact_id(resume_config, monkeypatch):
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', '')
    client = FakeClient([{'id': 'wrong', 'title': 'Other profession'}, {'id': 'target', 'title': 'QA'}])
    result = asyncio.run(apply_orchestrator.dispatch_apply(vacancy(), 'letter', hh_client=client))
    assert result['ok']
    assert client.calls[0]['preferred_resume_id'] == 'target'


def test_missing_target_does_not_use_only_account_resume(resume_config, monkeypatch):
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', '')
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_TITLE', '')
    client = FakeClient([{'id': 'wrong', 'title': 'Other profession'}])
    result = asyncio.run(apply_orchestrator.dispatch_apply(vacancy(), 'letter', hh_client=client))
    assert not result['ok']
    assert not client.calls


def test_retry_explicit_id_never_falls_back_to_primary(resume_config):
    client = FakeClient()
    asyncio.run(apply_orchestrator.dispatch_apply(vacancy(), 'letter', hh_client=client,
        preferred_resume_id='retry-target', preferred_resume_title='QA retry'))
    assert client.calls[0]['preferred_resume_id'] == 'retry-target'


def test_cached_foreign_variants_and_changed_primary_are_not_auto_selected(resume_config):
    from state_store.json_store import atomic_write_json
    atomic_write_json(config.HH_RESUME_PIPELINE_FILE, {'_resolved_variants': [
        {'name': 'normal', 'title': 'QA', 'id': 'stale-target'},
        {'name': 'foreign', 'title': 'Other profession', 'id': 'foreign'}]})
    assert pipeline.get_resolved_variants() == [{'name': 'normal', 'title': 'QA', 'id': 'target'}]


def test_title_resolution_refuses_duplicate_and_fuzzy_matches(resume_config, monkeypatch):
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', '')
    for resumes in ([{'id': 'a', 'title': 'QA'}, {'id': 'b', 'title': 'QA'}],
                    [{'id': 'a', 'title': 'QA engineer'}]):
        assert pipeline.resolve_variants(resumes)[0]['id'] == ''


def test_apply_without_explicit_id_never_navigates_or_clicks():
    class Page:
        async def goto(self, *args, **kwargs):
            pytest.fail('Missing exact resume ID must be blocked before navigation')

    async def noop(*args, **kwargs):
        return None

    result = asyncio.run(hh_apply.apply_to_vacancy(SimpleNamespace(_page=Page(), _save_debug_snapshot=noop),
        'https://hh.ru/vacancy/1', 'letter', preferred_resume_title='QA',
        absolute_hh_url=lambda x: x, anti_bot_message=lambda *args: '', logger=SimpleNamespace()))
    assert not result['ok']


class SubmitElement:
    def __init__(self, page):
        self.page = page
        self.attempts = 0

    async def wait_for_element_state(self, state, **kwargs):
        pass

    async def scroll_into_view_if_needed(self, **kwargs):
        pass

    async def evaluate(self, script, expected=None):
        if 'codex:action-ready' in script:
            return True
        if 'codex:action-dispatch' in script:
            await self.click()
            return {'id':expected['id'],'dispatched':True,'ok':True}
        if 'codex:native-destination-arm' in script:
            return self.page.url == expected['url']
        if 'codex:hh-submit-control' in script:
            return True
        if 'codex:hh-ui-inspect' in script:
            return []
        if 'dataQa:' in script:
            if self.page.switch_on_descriptor:
                self.page.selected = 'wrong'
            return {'tag': 'button', 'type': 'submit', 'dataQa': 'synthetic-submit'}
        return None

    async def click(self, **kwargs):
        self.attempts += 1
        if self.page.switch_on_failed_click:
            self.page.selected = 'wrong'
            raise RuntimeError('synthetic detached submit')
        self.page.sent = True


class LetterElement:
    def __init__(self):
        self.value = ''

    async def scroll_into_view_if_needed(self):
        pass

    async def fill(self, value):
        self.value = value

    async def type(self, value, **kwargs):
        self.value = value

    async def input_value(self):
        return self.value


class ResumeOption:
    def __init__(self, page):
        self.page = page

    async def inner_text(self):
        return 'QA'

    async def evaluate(self, script):
        if 'codex:hh-ui-inspect' in script:
            return []
        if 'value:' in script:
            return {'value': 'target', 'id': 'target'}
        return None

    async def click(self, **kwargs):
        self.page.selected = 'target'


class ApplyPage:
    context = fake_cdp_context()
    def __init__(self, selected='target', target_exists=True):
        self.url = ''
        self.selected = selected
        self.target_exists = target_exists
        self.sent = False
        self.switch_on_failed_click = False
        self.switch_on_descriptor = False
        self.submit = SubmitElement(self)
        self.letter = LetterElement()

    async def add_init_script(self, **kwargs):
        pass

    async def goto(self, url, **kwargs):
        self.url = url

    async def wait_for_timeout(self, value):
        pass

    async def content(self):
        return '<html><body>Synthetic response</body></html>'

    async def query_selector(self, selector):
        return None

    async def query_selector_all(self, selector):
        return [ResumeOption(self)] if self.target_exists else []

    async def evaluate(self, script, expected=None):
        if 'codex:action-disarm' in script:
            return None
        if 'codex:native-destination-readback' in script:
            return True
        if 'codex:hh-submit-arm' in script:
            return self.selected == expected['resume_id'] and self.letter.value == expected['cover_letter']
        if 'codex:hh-submit-readback' in script:
            return True
        if 'codex:hh-ui-inspect' in script:
            return []
        if 'const root' in script:
            return {'ids': [self.selected], 'titles': ['QA']}
        if 'form.requestSubmit' in script:
            self.sent = True
            return True
        raise AssertionError('Unexpected synthetic DOM script')


@pytest.mark.parametrize('scenario', ['switch_to_target', 'missing_target', 'late_switch', 'failed_click_switch', 'existing_topic'])
def test_full_apply_exact_target_and_fresh_guards(monkeypatch, scenario):
    from unittest.mock import AsyncMock
    page = ApplyPage('wrong' if scenario in ('switch_to_target', 'missing_target') else 'target',
                     target_exists=scenario != 'missing_target')
    page.switch_on_failed_click = scenario == 'failed_click_switch'
    page.switch_on_descriptor = scenario == 'late_switch'
    client = hh_client.HHClient()
    client._page = page
    client._save_debug_snapshot = AsyncMock()
    client._detect_anti_bot_kind = AsyncMock(return_value=None)
    client._handle_anti_bot_with_solver = AsyncMock(return_value=None)
    client._page_closed_or_archived = AsyncMock(return_value=False)
    client._has_existing_response_ui = AsyncMock(return_value=scenario == 'existing_topic')
    client._dismiss_magritte_dropdowns = AsyncMock()
    client._response_requires_questions = AsyncMock(return_value=False)
    client._inspect_employer_questions = AsyncMock(return_value={'fields': []})

    async def success():
        return page.sent or scenario == 'existing_topic'

    async def controls():
        return (page.url, object(), False, None, page.letter, page.submit)

    client._apply_success_detected = success
    client._detect_response_controls = controls
    result = asyncio.run(hh_apply.apply_to_vacancy(client, 'https://hh.ru/vacancy/1', 'letter',
        preferred_resume_id='target', preferred_resume_title='QA',
        absolute_hh_url=lambda x: x, anti_bot_message=lambda *args: '', logger=hh_client.log))
    if scenario == 'switch_to_target':
        assert result['ok'] and page.sent
        assert result['resume_selection_verified']
        assert result['selected_resume_id'] == 'target'
    elif scenario == 'existing_topic':
        assert not page.sent
        assert result['already_applied']
        assert not result['resume_selection_verified']
        assert result['selected_resume_id'] == ''
        assert result['resume_selection_status'] == 'unknown_existing_response'
    else:
        assert not result['ok']
        assert not page.sent
        assert page.submit.attempts == (1 if scenario == 'failed_click_switch' else 0)


def test_failed_fresh_title_resolution_does_not_restore_old_cached_id(resume_config, monkeypatch):
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', '')
    pipeline.remember_resolved_variants([{'name': 'normal', 'title': 'QA', 'id': 'previous'}])
    assert pipeline.get_resolved_variants()[0]['id'] == 'previous'
    pipeline.remember_resolved_variants(pipeline.resolve_variants([{'id': 'a', 'title': 'QA'}, {'id': 'b', 'title': 'QA'}]))
    assert pipeline.get_resolved_variants()[0]['id'] == ''


@pytest.mark.parametrize('selected', [None, [], {'ids': None}, {'ids': 'target'}, {'ids': [[]]}, {'ids': [123]}])
def test_unknown_identity_shape_fails_closed(selected):
    from unittest.mock import AsyncMock
    page = SimpleNamespace(evaluate=AsyncMock(return_value=selected))
    assert not asyncio.run(hh_apply.selected_resume_matches(page, 'target', 'QA'))


def test_title_target_is_captured_before_catalog_await(resume_config, monkeypatch):
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', '')
    client = FakeClient()

    async def catalog():
        monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_TITLE', 'Other profession')
        return [{'id': 'target', 'title': 'QA'}, {'id': 'wrong', 'title': 'Other profession'}]

    client.get_resume_ids = catalog
    result = asyncio.run(apply_orchestrator.dispatch_apply(vacancy(), 'letter', hh_client=client))
    assert result['ok']
    assert client.calls[0]['preferred_resume_id'] == 'target'
    assert client.calls[0]['preferred_resume_title'] == 'QA'


def test_blacklist_is_rechecked_after_resume_catalog_await(resume_config, monkeypatch):
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', '')
    blocked = [False]
    monkeypatch.setattr(apply_orchestrator.company_blacklist, 'is_blocked', lambda company: blocked[0])
    client = FakeClient()

    async def catalog():
        blocked[0] = True
        return [{'id': 'target', 'title': 'QA'}]

    client.get_resume_ids = catalog
    result = asyncio.run(apply_orchestrator.dispatch_apply(vacancy(), 'letter', hh_client=client))
    assert result['reason'] == 'company_blacklisted'
    assert not client.calls


def test_questionnaire_dom_failure_rechecks_id_before_selector_fallback():
    class Page(ApplyPage):
        async def query_selector(self, selector):
            return self.submit

        async def evaluate(self, script, arg=None):
            return await super().evaluate(script, arg)


    page = Page()
    client = hh_client.HHClient()
    client._page = page
    client._approved_hh_payload = {'resume_id':'target','cover_letter':'','answers':[]}
    original = page.submit.evaluate
    async def evaluate(script, arg=None):
        if 'codex:action-dispatch' in script:
            page.selected = 'wrong'
            return {'id':arg['id'],'dispatched':False,'ok':False}
        return await original(script, arg)
    page.submit.evaluate = evaluate

    async def guard():
        return await hh_apply.selected_resume_matches(page, 'target', 'QA')

    assert not asyncio.run(client._submit_employer_questions(before_submit=guard))
    assert not page.sent
    assert page.submit.attempts == 0
