"""Real callback and polling regressions from the independent load review."""
import asyncio
import threading
from unittest.mock import AsyncMock, Mock

import pytest

import analytics_read_cache
import directions
import profile as profile_mod
import telegram_bot
from telegram_app import guards, history
from tests.test_telegram_application_mode import mode_bot, add_direction
from tests.test_telegram_command_queue import spin_until
from tests.test_telegram_polling_health import bot


def button(bot, action):
    for call in reversed(bot._send_text.call_args_list):
        for row in (call.kwargs.get('reply_markup') or {}).get('inline_keyboard', []):
            for item in row:
                if item['callback_data'].split(':', 2)[-1] == action:
                    return item['callback_data']
    raise AssertionError(f'Missing rendered button {action}')


async def click(bot, data, uid=42):
    return await bot._handle_callback_query({'id': data, 'from': {'id': uid}, 'data': data,
        'message': {'message_id': 10, 'chat': {'id': uid, 'type': 'private'}}})


def test_rendered_checkbox_reusable_but_double_tap_old_revision_is_spent(mode_bot):
    bot, principal = mode_bot
    async def scenario():
        state = bot._direction_bind(principal, mode='catalog', selected=[], catalog=[{'id': '1', 'title': 'Synthetic'}])
        await bot._direction_show_catalog(42, state)
        old = button(bot, 'toggle:0')
        await asyncio.gather(click(bot, old), click(bot, old))
        assert state['selected'] == [0]
        assert bot._answer_callback_query.call_args.args[1] == guards.DONE
        current = button(bot, 'toggle:0')
        assert current != old
        await click(bot, current)
        assert state['selected'] == []
        await click(bot, old)
        assert state['selected'] == []
        assert bot._answer_callback_query.call_args.args[1] == guards.DONE
    asyncio.run(scenario())


def test_work_conditions_checkboxes_guard_and_cancel_through_real_router(mode_bot):
    bot, principal = mode_bot
    add_direction(bot)
    home = bot._profile('qa').home_dir
    original = directions.active_direction(home)
    async def scenario():
        await bot._dispatch_direction(42, principal, '/direction_edit', 'work_formats')
        await click(bot, button(bot, 'workremote'))
        state = bot._direction_sessions()[42]
        remote = button(bot, 'worktoggle:remote')
        assert await click(bot, remote) is False
        assert 'Оставьте хотя бы один формат работы' in bot._send_text.call_args.args[1]
        assert state['draft']['work_conditions']['allowed_formats'] == ['remote']
        hybrid = button(bot, 'worktoggle:hybrid')
        await asyncio.gather(click(bot, hybrid), click(bot, hybrid))
        assert state['draft']['work_conditions']['allowed_formats'] == ['remote', 'hybrid']
        assert bot._answer_callback_query.call_args.args[1] == guards.DONE
        await click(bot, button(bot, 'worktoggle:hybrid'))
        assert state['draft']['work_conditions']['allowed_formats'] == ['remote']
        await click(bot, button(bot, 'flytoggle'))
        await click(bot, button(bot, 'flytoggle'))
        assert not state['draft']['work_conditions']['allow_fly_in_fly_out']
        await click(bot, button(bot, 'cancel'))
        assert bot._direction_sessions()[42] is not state
        assert bot._direction_sessions()[42]['draft'] == original
        await click(bot, button(bot, 'editmenu'))
        await click(bot, button(bot, 'edit:work_formats'))
        await click(bot, button(bot, 'workremote'))
        assert bot._direction_sessions()[42]['draft']['work_conditions']['allowed_formats'] == ['remote']
        assert directions.active_direction(home) == original
    asyncio.run(scenario())


def test_direction_navigation_can_be_repeated_on_new_screens(mode_bot):
    bot, principal = mode_bot
    add_direction(bot)
    async def scenario():
        await bot._dispatch_direction(42, principal, '/direction_summary', '')
        for _ in range(3):
            await click(bot, button(bot, 'strategy'))
            await click(bot, button(bot, 'back'))
            assert 'Каким резюме откликаться' in bot._send_text.call_args.args[1]
        assert not bot._load_state().get('callback_claims')
    asyncio.run(scenario())


def test_rejected_next_is_released_and_works_after_selection(mode_bot, monkeypatch):
    bot, principal = mode_bot
    job = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, '_direction_job', job)
    async def scenario():
        state = bot._direction_bind(principal, mode='catalog', selected=[], catalog=[{'id': '1', 'title': 'Synthetic'}])
        await bot._direction_show_catalog(42, state)
        assert await click(bot, button(bot, 'next')) is False
        assert 'Выберите хотя бы одно' in bot._send_text.call_args.args[1]
        assert not bot._load_state().get('callback_claims')
        await click(bot, button(bot, 'toggle:0'))
        await click(bot, button(bot, 'next'))
        assert job.await_count == 1
        assert job.call_args.args[-1] == [state['catalog'][0]]
    asyncio.run(scenario())


@pytest.mark.parametrize('uncertain', [False, True])
def test_busy_delete_can_retry_and_completed_or_uncertain_delete_stays_spent(mode_bot, monkeypatch, uncertain):
    bot, principal = mode_bot
    add_direction(bot)
    busy = [False]
    monkeypatch.setattr(bot, '_has_active_command', lambda name: busy[0])
    original = directions.delete_direction
    def delete(*args, **kwargs):
        original(*args, **kwargs)
        if uncertain:
            raise OSError('synthetic error after mutation')
    deletion = Mock(side_effect=delete)
    monkeypatch.setattr(directions, 'delete_direction', deletion)
    async def scenario():
        await bot._dispatch_direction(42, principal, '/direction_summary', '')
        await click(bot, button(bot, 'delete'))
        confirmation = button(bot, 'deleteyes')
        busy[0] = True
        assert await click(bot, confirmation) is False
        assert not bot._load_state().get('callback_claims')
        deletion.assert_not_called()
        busy[0] = False
        await click(bot, confirmation)
        assert deletion.call_count == 1
        await click(bot, confirmation)
        assert deletion.call_count == 1
        assert bot._answer_callback_query.call_args.args[1] == guards.DONE
    asyncio.run(scenario())


def test_busy_auto_confirmation_keeps_prompt_for_retry(mode_bot, monkeypatch):
    bot, principal = mode_bot
    add_direction(bot)
    busy = [False]
    monkeypatch.setattr(bot, '_has_active_command', lambda name: busy[0])
    async def scenario():
        await bot._request_application_mode(42, principal, 'auto')
        confirmation = button(bot, 'autoyes')
        state = bot._direction_sessions()[42]
        busy[0] = True
        assert await click(bot, confirmation) is False
        assert bot._direction_sessions()[42] is state
        assert bot._profile('qa').hh.application_mode == 'confirm'
        busy[0] = False
        await click(bot, confirmation)
        assert bot._profile('qa').hh.application_mode == 'auto'
        await click(bot, confirmation)
        assert bot._answer_callback_query.call_args.args[1] == guards.DONE
    asyncio.run(scenario())


def test_due_daily_summary_waiting_on_stats_index_does_not_block_polling_start(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(bot, '_send_start_summary', AsyncMock(return_value=False))
    monkeypatch.setattr(bot, '_recover_manual_applies', lambda: None)
    profile = profile_mod.Profile(name='synthetic', home_dir=str(tmp_path), resume_file=str(tmp_path / 'resume.md'))
    principal = {'user_id': 42, 'role': 'admin', 'profile': 'synthetic'}
    monkeypatch.setattr(bot, '_profile', lambda name: profile)
    monkeypatch.setattr(bot, '_profile_names', lambda: ['synthetic'])
    monkeypatch.setattr(bot, '_selected_profile', lambda user: 'synthetic')
    monkeypatch.setattr(telegram_bot.telegram_access, 'resolve_user', lambda uid: principal)
    monkeypatch.setattr(telegram_bot.telegram_access, 'list_users', lambda: [principal])
    monkeypatch.setattr(bot, '_daily_summary_due', lambda: (True, '2026-10-23'))
    monkeypatch.setattr(bot, '_send_text', AsyncMock(return_value={}))
    monkeypatch.setattr(bot, '_send_text_safely', bot._send_text)
    monkeypatch.setattr(bot, '_menu_reply_markup', lambda *args, **kwargs: {})
    monkeypatch.setattr(bot, '_send_getting_started', AsyncMock())
    monkeypatch.setattr(bot, '_maybe_send_health_alert', AsyncMock())
    stats_entered, daily_entered, finish = threading.Event(), threading.Event(), threading.Event()
    index = analytics_read_cache.journal(profile.analytics_events_file)
    def stats(*args):
        with index.lock:
            stats_entered.set()
            assert finish.wait(5)
        return 'synthetic', ['stats complete'], None
    monkeypatch.setattr(history, '_history_report', stats)
    original = bot._daily_summary_report
    def report(*args, **kwargs):
        assert stats_entered.wait(5)
        daily_entered.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(bot, '_daily_summary_report', report)
    async def scenario():
        monkeypatch.setattr(asyncio.get_running_loop(), 'add_signal_handler', lambda *args: None)
        started = asyncio.Event()
        async def menu(*args, **kwargs):
            started.set()
        monkeypatch.setattr(bot, '_send_menu', menu)
        polls = []
        async def poll():
            polls.append(True)
            if len(polls) == 1:
                text = '/stats'
            elif len(polls) == 2:
                await spin_until(daily_entered.is_set)
                text = '/start'
            else:
                await bot._stop_event.wait()
                return []
            return [{'update_id': len(polls), 'message': {'from': {'id': 42},
                     'chat': {'id': 42, 'type': 'private'}, 'text': text}}]
        monkeypatch.setattr(bot, '_get_updates', poll)
        running = asyncio.create_task(bot.run())
        try:
            await asyncio.wait_for(started.wait(), 1)
            assert stats_entered.is_set() and daily_entered.is_set() and not finish.is_set()
            assert not bot._daily_summary_report_task.done()
        finally:
            finish.set()
            bot._stop_event.set()
            await asyncio.wait_for(running, 3)
    asyncio.run(scenario())


def test_daily_timeout_does_not_start_more_report_workers(bot, monkeypatch):
    entered, finish = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(bot, '_daily_summary_due', lambda: (True, '2026-10-23'))
    monkeypatch.setattr(bot, '_profile_names', lambda: ['synthetic'])
    monkeypatch.setattr(telegram_bot.telegram_access, 'list_users', lambda: [{'user_id': 42, 'role': 'admin', 'profile': 'synthetic'}])
    def report(*args, **kwargs):
        calls.append(True)
        entered.set()
        assert finish.wait(5)
        return 'daily'
    monkeypatch.setattr(bot, '_daily_summary_report', report)
    async def scenario():
        bot.UPDATE_TIMEOUT_SECONDS = 0.03
        bot.SLOW_UPDATE_SECONDS = 0.01
        bot._schedule_daily_summary()
        try:
            await spin_until(entered.is_set)
            await bot._daily_summary_task
            for _ in range(4):
                bot._schedule_daily_summary()
            assert calls == [True]
            assert not bot._daily_summary_report_task.done()
        finally:
            finish.set()
            await bot._daily_summary_report_task
            await bot._shutdown_read_updates()
    asyncio.run(scenario())


def test_direction_delete_rejects_changed_confirmation_and_keeps_resume_texts(tmp_path):
    from tests.test_telegram_directions import make_direction
    first = make_direction(tmp_path, 'qa')
    second = make_direction(tmp_path, 'other')
    directions.set_active_direction(tmp_path, 'qa')
    changed = {**first, 'title': 'Changed'}
    directions.save_direction(tmp_path, changed, texts={})
    with pytest.raises(directions.DirectionError, match='изменилось'):
        directions.delete_direction(tmp_path, 'qa', expected=first)
    assert directions.active_direction(tmp_path)['title'] == 'Changed'
    text_paths = [v['text_file'] for v in second['variants']]
    directions.delete_direction(tmp_path, 'other', expected=second)
    assert directions.active_direction(tmp_path)['name'] == 'qa'
    from pathlib import Path
    assert all(Path(path).is_file() for path in text_paths)
    directions.delete_direction(tmp_path, 'qa', expected=directions.load_direction(tmp_path, 'qa'))
    assert directions.active_direction(tmp_path) is None


def test_direction_delete_denies_tester_scope_without_writes(tmp_path, monkeypatch):
    import profile_access
    from tests.test_telegram_directions import make_direction
    value = make_direction(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    monkeypatch.setattr(profile_access, 'current_scope', lambda: ('synthetic-dev', 'qa', '42'))
    with pytest.raises(PermissionError, match='удалять'):
        directions.delete_direction(tmp_path, 'qa', expected=value)
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('uncertain', [False, True])
def test_auto_write_validation_retry_and_uncertain_write_claim(mode_bot, monkeypatch, uncertain):
    bot, principal = mode_bot
    add_direction(bot)
    original = directions.save_direction
    failing = [True]
    def save(*args, **kwargs):
        if failing[0]:
            if uncertain:
                original(*args, **kwargs)
                raise OSError('synthetic error after commit')
            raise ValueError('synthetic validation rejection')
        return original(*args, **kwargs)
    monkeypatch.setattr(directions, 'save_direction', save)
    async def scenario():
        await bot._request_application_mode(42, principal, 'auto')
        confirmation = button(bot, 'autoyes')
        result = await click(bot, confirmation)
        if uncertain:
            assert result is None
            assert bot._profile('qa').hh.application_mode == 'auto'
            await click(bot, confirmation)
            assert bot._answer_callback_query.call_args.args[1] == guards.DONE
        else:
            assert result is False
            assert bot._profile('qa').hh.application_mode == 'confirm'
            failing[0] = False
            await click(bot, confirmation)
            assert bot._profile('qa').hh.application_mode == 'auto'
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['autono', 'cancel'])
def test_completed_cancellation_keeps_claim_when_delivery_fails(mode_bot, action):
    bot, principal = mode_bot
    add_direction(bot)
    async def scenario():
        if action == 'autono':
            await bot._request_application_mode(42, principal, 'auto')
        else:
            state = bot._direction_bind(principal, mode='draft', draft=directions.active_direction(bot._profile('qa').home_dir))
            await bot._direction_show_draft(42, principal, state)
        data = button(bot, action)
        # The action completes, its acknowledgement fails, then the error reply succeeds.
        bot._send_text.side_effect = [OSError('synthetic delivery failure'), {}]
        assert await click(bot, data) is None
        assert 42 not in bot._direction_sessions()
        await click(bot, data)
        assert bot._answer_callback_query.call_args.args[1] == guards.DONE
    asyncio.run(scenario())
