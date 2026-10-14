"""Frame presence is telemetry; actual unowned side effects remain sticky."""
import asyncio
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from hh.recovery import install_frame_observation, monitor_page


@pytest.mark.parametrize('side_effect', ['none', 'post', 'get', 'click', 'submit'])
def test_hidden_frame_only_becomes_uncertain_on_unowned_post(side_effect, caplog):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(service_workers='block')
            async def offline(route):
                if route.request.url == 'https://hh.ru/vacancy/2':
                    await route.fulfill(status=200, content_type='text/html', body='<h1>Synthetic</h1>')
                else:
                    await route.abort()
            await context.route('**/*', offline)
            session = SimpleNamespace(_context=context)
            await install_frame_observation(session, context)
            page = await context.new_page()
            session._page = page
            monitor = monitor_page(session, page)
            await page.goto('https://hh.ru/vacancy/2')
            await page.evaluate("""() => {
                const frame=document.createElement('iframe'); frame.hidden=true;
                frame.srcdoc='<body>Passive synthetic frame</body>';
                document.body.appendChild(frame);
            }""")
            await page.wait_for_timeout(50)
            assert len(page.frames) == 2
            assert monitor.unknown is False
            assert context._hh_action_watch.unknown is False
            assert not getattr(session, '_hh_recovery_uncertain', False)
            assert not getattr(session, '_hh_recovery_stop_reason', '')
            assert 'frame attached' in caplog.text.lower()
            if side_effect != 'none':
                frame = next(frame for frame in page.frames if frame is not page.main_frame)
                if side_effect == 'post':
                    await frame.evaluate("""() => fetch('https://hh.ru/applicant/vacancy_response/popup', {
                        method:'POST', body:new URLSearchParams({vacancy_id:'2'})
                    }).catch(() => null)""")
                elif side_effect == 'get':
                    await frame.evaluate("""() => fetch('https://hh.ru/applicant/vacancy_response?resume_id=other').catch(() => null)""")
                elif side_effect == 'click':
                    await frame.evaluate("""() => {
                        window.__jhHHFrameInteraction=() => Promise.resolve();
                        const button=document.createElement('button');
                        document.body.appendChild(button); button.click();
                    }""")
                else:
                    await frame.evaluate("""() => {
                        const form=document.createElement('form');
                        document.body.appendChild(form);
                        form.dispatchEvent(new Event('submit', {bubbles:true,cancelable:true}));
                    }""")
                await page.wait_for_timeout(50)
                assert monitor.unknown is True
                assert context._hh_action_watch.unknown is True
                assert session._hh_recovery_uncertain is True
                assert context._hh_action_watch.application_post_sequence == (1 if side_effect == 'post' else 0)
            await browser.close()
    asyncio.run(run())
