"""Offline UI characterization: unknown containment cannot authorize interaction."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from hh.ui import HHUIGuard, HHUnexpectedUI

FORM = '''<form name="vacancy_response"><input name="resume_id" value="exact-target">
<div data-qa="resume-title">Synthetic QA</div>
<button type="submit" data-qa="vacancy-response-submit-popup">Apply</button></form>'''
CLOSE = '<button type="button" aria-label="Закрыть" data-qa="modal-close" onclick="window.interactions.push(\'close\');this.closest(\'[role=dialog]\').remove()">X</button>'
OPTIONAL = '<div role="dialog"><h2>Резюме стали компактнее</h2>' + CLOSE + '</div>'
PICKER = '''<div role="dialog" data-qa="drop-base"><div data-qa="drop">
<div role="listbox" data-qa="magritte-select-option-list">
<label role="option" data-magritte-select-option="exact-target"><input type="radio" value="exact-target" checked><span data-qa="resume-title">Synthetic QA</span></label>
</div></div></div>'''


async def browser_case(html, check):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            '--disable-background-networking', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'])
        try:
            context = await browser.new_context(service_workers='block')
            requests = []
            async def abort(route):
                requests.append(route.request.url)
                await route.abort()
            await context.route('**/*', abort)
            page = await context.new_page()
            await page.set_content(html)
            await page.evaluate('''() => {
                window.interactions=[];
                for (const name of ['click','submit','formdata']) document.addEventListener(name,event=>{
                    window.interactions.push(name); if(name==='submit')event.preventDefault();
                },true);
            }''')
            await check(page)
            assert requests == []
        finally:
            await browser.close()


@pytest.mark.parametrize('html,allowed,closed', [
    ('<div role="dialog">' + FORM + '</div>', ('response',), False),
    ('<div role="dialog">' + FORM.replace('name="vacancy_response"', 'id="actual"') + '</div><input type="hidden" form="actual" name="consent" value="reviewed">', ('response',), False),
    ('<div role="dialog" data-qa="vacancy-response-popup">' + FORM + CLOSE + '</div>', ('response',), False),
    (FORM + PICKER, ('response',), False),
    (OPTIONAL, (), True),
    ('<div data-qa="modal-overlay">' + OPTIONAL + '</div>', (), True),
])
def test_proven_existing_surface_compatibility(tmp_path, html, allowed, closed):
    async def check(page):
        notify = AsyncMock()
        guard = HHUIGuard(tmp_path, notify=notify)
        assert await guard.ensure(page, 'characterization', allowed=allowed) is closed
        events = await page.evaluate('window.interactions')
        assert events == ['click', 'close'] if closed else events == []
        notify.assert_not_awaited()
    asyncio.run(browser_case(html, check))


@pytest.mark.parametrize('html', [
    '<div role="dialog" data-qa="unknown-consent">' + FORM + CLOSE + '</div>',
    '<div role="dialog" data-qa="unknown-consent">' + FORM.replace('name="vacancy_response"', 'id="actual"') + CLOSE + '</div>',
    '<div role="dialog">' + FORM.replace('name="vacancy_response"', 'name="profile"') + '</div>',
    '<div role="dialog" data-qa="vacancy-response-popup"><div role="dialog" data-qa="unknown-consent">' + FORM + '</div>' + CLOSE + '</div>',
    '<div role="dialog"><div role="dialog" data-qa="unknown-consent">' + FORM + '</div>' + CLOSE + '</div>',
    OPTIONAL.replace(CLOSE, '<div role="dialog">Unknown consent</div>' + CLOSE),
    OPTIONAL.replace(CLOSE, '<form name="profile"><input name="private-answer"></form>' + CLOSE),
    OPTIONAL.replace(CLOSE, '<button type="submit">Save profile</button>' + CLOSE),
    OPTIONAL.replace('role="dialog"', 'role="dialog" data-qa="unknown-consent"', 1),
    '<div data-qa="modal-overlay">Unknown consent' + OPTIONAL + '</div>',
    '<div role="dialog" data-qa="vacancy-response-popup">' + FORM + 'Unknown consent' + CLOSE + '</div>',
    '<div role="dialog" data-qa="vacancy-response-popup">' + FORM + '<p>Unknown consent</p>' + CLOSE + '</div>',
    FORM + PICKER.replace('</div></div></div>', '<p>Unknown consent</p></div></div></div>'),
    '<div role="dialog" data-qa="applicant-profile-onboarding-modal"><h2>Расскажите о себе</h2><div role="dialog">Unknown consent</div>' + CLOSE + '</div>',
    '<div role="dialog" data-qa="applicant-profile-onboarding-modal"><h2>Расскажите о себе</h2>' + FORM + CLOSE + '</div>',
    '<div data-qa="applicant-profile-onboarding-modal"><h2>Расскажите о себе</h2>' + FORM + CLOSE + '</div>',
    '<div role="dialog" data-qa="vacancy-response-popup"><div data-qa="applicant-profile-completion-modal">' + FORM + '</div></div>',
])
def test_unknown_wrapped_nested_or_mixed_surface_never_interacted(tmp_path, html):
    async def check(page):
        guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
        before = await page.locator('body').inner_text()
        with pytest.raises(HHUnexpectedUI):
            await guard.ensure(page, 'isolation', allowed=('response',))
        assert await page.locator('body').inner_text() == before
        assert await page.evaluate('window.interactions') == []
    asyncio.run(browser_case(html, check))


@pytest.mark.parametrize('challenge', [
    '<div data-qa="captcha">Challenge</div>', '<div class="SmartCaptcha">Challenge</div>',
    '<div id="hh-captcha">Challenge</div>', '<iframe src="about:blank#captcha"></iframe>',
])
def test_challenge_precedes_optional_or_response_classification(tmp_path, challenge):
    html = '<div role="dialog" data-qa="vacancy-response-popup">' + FORM + challenge + '</div>'
    async def check(page):
        guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
        scan = await guard._scan(page, ())
        assert scan[0]['kind'] == 'captcha'
        with pytest.raises(HHUnexpectedUI):
            await guard.ensure(page, 'challenge', allowed=('response',))
        assert await page.evaluate('window.interactions') == []
    asyncio.run(browser_case(html, check))


@pytest.mark.parametrize('marker,title', [
    ('applicant-profile-onboarding-modal', 'Расскажите о себе'),
    ('applicant-profile-completion-modal', 'Заполните профиль'),
])
def test_pure_profile_modal_never_clicked_by_user_policy(tmp_path, marker, title):
    html = '<div role="dialog" data-qa="' + marker + '"><h2>' + title + '</h2><input name="profile-answer">' + CLOSE + '</div>'
    async def check(page):
        guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
        with pytest.raises(HHUnexpectedUI):
            await guard.ensure(page, 'profile_isolation')
        assert await page.locator('[name="profile-answer"]').input_value() == ''
        assert await page.evaluate('window.interactions') == []
    asyncio.run(browser_case(html, check))


@pytest.mark.parametrize('content', [
    'Unknown consent requires publication', '<p>Unknown consent requires publication</p>',
    '<button type="button">Accept consent</button>', '<div role="button">Accept consent</div>',
    '<input type="hidden" name="profile-consent" value="yes">',
])
def test_news_title_does_not_authorize_other_content(tmp_path, content):
    html = OPTIONAL.replace(CLOSE, content + CLOSE)
    async def check(page):
        guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
        with pytest.raises(HHUnexpectedUI):
            await guard.ensure(page, 'mixed_news')
        assert await page.evaluate('window.interactions') == []
    asyncio.run(browser_case(html, check))
