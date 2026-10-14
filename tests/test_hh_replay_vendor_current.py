"""Offline coverage for the currently captured HH replay timer dependency."""
import asyncio

import pytest

from hh import apply
from hh.ui import HHUnexpectedUI
from tests.test_hh_response_navigation import (
    FORM_HTML,
    URL,
    VENDOR_REPLAY_JS,
    VENDOR_URL,
    browser_case,
    configured_response_navigation,
)

CURRENT_VENDOR_URL = 'https://i.hh.ru/build/vendors.91818c9dc6b48eb9.js'


def response_with_vendor(script_url):
    old_html = FORM_HTML.replace('</body>', f'<script src="{VENDOR_URL}"></script></body>')
    return old_html.replace(VENDOR_URL, script_url)


def test_current_replay_timer_preserves_response_without_attaching_frame(tmp_path, monkeypatch):
    monkeypatch.delenv('HH_RESPONSE_FIX_DISABLED', raising=False)

    async def check(client, page, context, blocked):
        attached = []
        page.on('frameattached', lambda frame: attached.append(frame))
        guard_error = None
        try:
            async with configured_response_navigation(client):
                await page.goto(URL, wait_until='domcontentloaded')
                await page.wait_for_timeout(100)
                assert await page.evaluate('window.__replayTimerCallbackRan') is True
                assert attached == [], 'known Wu helper attached a frame before suppression'
                current, header, questions, resume, letter, submit = await apply.detect_response_controls(client)
                assert current == URL and not questions
                assert resume is not None and letter is not None and submit is not None
                assert await apply.selected_resume_matches(page, 'qa-target', '')
                await letter.fill('Synthetic offline cover letter')
                assert await letter.input_value() == 'Synthetic offline cover letter'
                assert await submit.is_enabled()
        except HHUnexpectedUI as exc:
            guard_error = exc

        assert not client._page._hh_action_monitor.unknown
        assert not context._hh_action_watch.unknown
        assert guard_error is None
        assert blocked == [], 'offline response initialization attempted a POST or other write'

    asyncio.run(browser_case(
        tmp_path, monkeypatch, check,
        response_html=response_with_vendor(CURRENT_VENDOR_URL),
        resources={CURRENT_VENDOR_URL: VENDOR_REPLAY_JS},
    ))


@pytest.mark.parametrize('variant', ['other_url', 'other_function', 'extra_attribute', 'other_actor'])
def test_current_vendor_unrecognized_passive_frames_are_observed(tmp_path, monkeypatch, variant):
    monkeypatch.delenv('HH_RESPONSE_FIX_DISABLED', raising=False)
    script_url = CURRENT_VENDOR_URL
    script = VENDOR_REPLAY_JS
    if variant == 'other_url':
        script_url = CURRENT_VENDOR_URL.replace('91818c9dc6b48eb9', '0000000000000000')
    elif variant == 'other_function':
        script = script.replace('Wu', 'OtherTimer')
    elif variant == 'extra_attribute':
        script = script.replace('t.hidden = !0;', 't.hidden = !0; t.setAttribute("data-extra", "x");')
    elif variant == 'other_actor':
        script += '''
function OtherActor() {
  const frame = document.createElement('iframe');
  frame.hidden = true;
  document.head.appendChild(frame);
  document.head.removeChild(frame);
}
OtherActor();
'''

    async def check(client, page, context, blocked):
        attached = []
        page.on('frameattached', lambda frame: attached.append(frame))
        guard_error = None
        try:
            async with configured_response_navigation(client):
                await page.goto(URL, wait_until='domcontentloaded')
                await page.wait_for_timeout(100)
                assert await page.evaluate('window.__replayTimerCallbackRan') is True
        except HHUnexpectedUI as exc:
            guard_error = exc

        assert attached, variant
        assert client._page._hh_action_monitor.embedded_history, variant
        assert not client._page._hh_action_monitor.unknown, variant
        assert not context._hh_action_watch.unknown, variant
        assert guard_error is None, variant
        assert context._hh_action_watch.application_post_sequence == 0
        assert blocked == []

    asyncio.run(browser_case(
        tmp_path, monkeypatch, check,
        response_html=response_with_vendor(script_url),
        resources={script_url: script},
    ))
