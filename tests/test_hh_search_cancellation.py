"""Offline browser cancellation must drain protocol futures before closing."""
import asyncio
import gc
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from hh_client import HHClient
import config


def test_cancelled_native_search_does_not_leave_target_closed_future(monkeypatch):
    async def run():
        errors = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: errors.append(context))
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context()
                await context.route('**/*', lambda route: route.abort())
                page = await context.new_page()
                started = asyncio.Event()
                async def blocked_goto(*args, **kwargs):
                    started.set()
                    await page.wait_for_timeout(30000)
                monkeypatch.setattr(page, 'goto', blocked_goto)
                client = HHClient()
                client._page = page
                monkeypatch.setattr(client, '_ensure_expected_ui', AsyncMock())
                async def workflow():
                    try:
                        await client.search_vacancies('synthetic')
                    finally:
                        await browser.close()
                task = asyncio.create_task(workflow())
                await asyncio.wait_for(started.wait(), timeout=5)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                gc.collect()
                await asyncio.sleep(0)
            gc.collect()
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous)
        assert not errors, [(row.get('message'), type(row.get('exception')).__name__) for row in errors]
    asyncio.run(run())


def test_search_query_config_is_captured_before_child_task_scheduling(monkeypatch):
    from urllib.parse import parse_qs, urlparse
    monkeypatch.setattr(config, 'HH_BASE_URL', 'https://original.invalid')
    monkeypatch.setattr(config, 'SEARCH_EXPERIENCE', 'between1And3')
    monkeypatch.setattr(config, 'SEARCH_SALARY', 12345)
    monkeypatch.setattr(config, 'SEARCH_ONLY_WITH_SALARY', True)
    client = HHClient()
    captured = []
    async def search(query, *, page, url):
        captured.append(url)
        return []
    monkeypatch.setattr(client, '_search_vacancies', search)
    async def switch():
        monkeypatch.setattr(config, 'HH_BASE_URL', 'https://foreign.invalid')
        monkeypatch.setattr(config, 'SEARCH_EXPERIENCE', 'moreThan6')
        monkeypatch.setattr(config, 'SEARCH_SALARY', 99999)
        monkeypatch.setattr(config, 'SEARCH_ONLY_WITH_SALARY', False)
    async def run():
        switched = asyncio.create_task(switch())
        await client.search_vacancies('synthetic', area=1, schedule='remote')
        await switched
    asyncio.run(run())
    assert urlparse(captured[0]).netloc == 'original.invalid'
    query = parse_qs(urlparse(captured[0]).query)
    assert query['experience'] == ['between1And3'] and query['salary'] == ['12345']
    assert query['only_with_salary'] == ['true'] and query['schedule'] == ['remote']
