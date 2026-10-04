"""Synthetic destination URLs in Chromium; all routing stays offline."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright
import config
from habr_career_client import HabrCareerClient
from superjob_client import SuperJobClient


@pytest.mark.parametrize('source', ['habr', 'superjob'])
@pytest.mark.parametrize('navigation', ['failed', 'wrong_url', 'foreign_host', 'changed_during_login', 'correct'])
def test_only_verified_destination_can_reach_apply_controls(tmp_path, monkeypatch, source, navigation):
    monkeypatch.setattr(config, 'HABR_COOKIES_FILE', str(tmp_path / 'habr.json'))
    monkeypatch.setattr(config, 'SUPERJOB_COOKIES_FILE', str(tmp_path / 'sj.json'))
    monkeypatch.setattr(config, 'SUPERJOB_AUTH_FILE', str(tmp_path / 'sj-auth.json'))
    monkeypatch.setattr(config, 'HH_STATE_DIR', str(tmp_path / 'state'))
    base = 'https://career.habr.com/vacancies/' if source == 'habr' else 'https://www.superjob.ru/vakansii/qa-'
    suffix = '' if source == 'habr' else '.html'
    old, approved = base + '1' + suffix, base + '2' + suffix
    html = '''<meta charset="utf-8"><button class="f-test-vacancy-response-button" onclick="window.actions++">Откликнуться</button>
      <button type="submit" class="f-test-button-Otkliknutsya" onclick="window.actions++">Отправить</button>
      <script>window.actions=0</script>'''
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
                page = await context.new_page()
                await page.goto(old)
                original_goto = page.goto
                async def goto(url, **kwargs):
                    if navigation == 'failed': raise TimeoutError('before destination changed')
                    if navigation == 'wrong_url': return await original_goto(old, **kwargs)
                    if navigation == 'foreign_host':
                        foreign = approved.replace('career.habr.com', 'career.habr.com.evil.invalid').replace('www.superjob.ru', 'superjob.ru.evil.invalid')
                        return await original_goto(foreign, **kwargs)
                    return await original_goto(url, **kwargs)
                page.goto = goto
                page.wait_for_timeout = AsyncMock()
                client = HabrCareerClient() if source == 'habr' else SuperJobClient()
                client._page = page
                async def logged_in():
                    if navigation == 'changed_during_login': await original_goto(old)
                    return True
                client._page_is_logged_in = logged_in
                if source == 'habr': result = await client.apply_to_vacancy(approved)
                else: result = await client.apply_to_vacancy({'url': approved, 'external_id': '2'})
                actions = await page.evaluate('() => window.actions')
                assert (actions > 0 if navigation == 'correct' else actions == 0), result
            finally: await browser.close()
    asyncio.run(run())


def test_superjob_approved_id_must_match_approved_url(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(config, 'SUPERJOB_AUTH_FILE', str(tmp_path / 'auth.json'))
    client = SuperJobClient()
    client._page = SimpleNamespace(goto=AsyncMock())
    result = asyncio.run(client.apply_to_vacancy({'url': 'https://www.superjob.ru/vakansii/qa-2.html', 'id': 'superjob:1'}))
    assert not result['ok']
    client._page.goto.assert_not_awaited()
