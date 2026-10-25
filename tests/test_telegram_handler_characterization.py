"""Frozen outcomes for runner branches and direction commands before split-22c.

Only network/process/AI boundaries are synthetic. Texts, markups, documents,
command results, cleanup, authorization calls and draft/file writes are retained.
The fixtures were recorded on the original handler bodies; do not regenerate
these expectations to accommodate extraction changes.
"""
import asyncio
import inspect
import json
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import directions
import telegram_bot
from tests.test_telegram_dispatch_characterization import normalize, profile_files
from tests.telegram_f27_expectations import f27_expected
from tests.test_telegram_menu_routing import menu_bot  # noqa: F401
from tests.test_telegram_directions import Harness, USER, make_direction

SNAPSHOT = Path(__file__).with_name('fixtures') / 'telegram_handlers.json'


def frozen(key, actual):
    expected = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    assert actual == f27_expected("handlers", key, expected[key])


RUNNERS = ('search', 'dryrun', 'analyze', 'cli', 'form', 'list', 'reply', 'send', 'manual')
OUTCOMES = ('success', 'failed', 'cancelled', 'error', 'denied')


@pytest.mark.parametrize('kind', RUNNERS)
@pytest.mark.parametrize('outcome', OUTCOMES)
def test_runner_outcomes(menu_bot, monkeypatch, kind, outcome):
    bot, workers = menu_bot
    original_mark = bot._mark_active_command
    def mark(**kwargs):
        active = original_mark(**kwargs)
        active.started_at = 123.0
        active.cancel_requested = False
        return active
    monkeypatch.setattr(bot, '_mark_active_command', mark)
    principal = {'user_id': 42, 'role': 'admin', 'profile': 'qa'}
    audit, cleared, authorized, captures = [], [], [], []
    import chat_context
    monkeypatch.setattr(chat_context, 'safe_resolve', lambda *a: SimpleNamespace(
        status=chat_context.OWN, resume_id='synthetic-resume', direction=None))
    monkeypatch.setattr(bot, '_append_chat_ai_audit_event', lambda *a, **kw: audit.append([a, kw]))
    monkeypatch.setattr(bot, '_clear_active_command', lambda name: cleared.append(name))
    monkeypatch.setattr(bot, '_delete_message', AsyncMock())
    monkeypatch.setattr(bot, '_send_text', AsyncMock(return_value={'message_id': 7}))
    monkeypatch.setattr(bot, '_send_text_safely', bot._send_text)
    monkeypatch.setattr(bot, '_run_progress_indicator', AsyncMock())
    monkeypatch.setattr(telegram_bot.telegram_resume_limits, 'record_resume_analysis',
                        lambda *a, **kw: audit.append(['analysis', a, kw]))
    monkeypatch.setattr(telegram_bot.manual_apply_queue, 'get_candidate', lambda *a, **kw: {})
    async def authorize(*a, **kw):
        authorized.append(kw)
        return outcome != 'denied'
    monkeypatch.setattr(bot, '_authorize_ai_usage', authorize)
    async def capture(command, argv, **kw):
        captures.append([['<python>' if part == sys.executable else part for part in argv], kw])
        if outcome == 'cancelled':
            raise asyncio.CancelledError()
        if outcome == 'error':
            raise RuntimeError('Synthetic capture failure')
        return {'ok': outcome != 'failed', 'returncode': 0 if outcome != 'failed' else 1,
                'stdout': json.dumps({'chat_candidates': {'candidates': [], 'chats_scanned': 3, 'chats_read': 2}}),
                'stderr': 'Synthetic error' if outcome == 'failed' else ''}
    monkeypatch.setattr(bot, '_run_active_command_capture', capture)
    async def run():
        if kind in {'search', 'dryrun', 'analyze', 'cli'}:
            flag, label = {'search': ('--search', 'search'), 'dryrun': ('--dry-run', 'dry-run'),
                           'analyze': ('--analyze-resume', 'analyze resume'), 'cli': ('--check', 'check invitations')}[kind]
            result = await inspect.unwrap(bot._start_cli_command)(bot, 42, principal, 'qa', flag, label, 120)
        else:
            name, kwargs = {
                'form': ('_start_google_form_command', {'label': 'google form preview', 'argv': ['synthetic', '--google-form-preview']}),
                'list': ('_start_chat_ai_candidate_list', {}),
                'reply': ('_start_chat_ai_reply', {'hh_chat_id': '123456', 'hh_message_id': '234567'}),
                'send': ('_start_chat_ai_reply', {'hh_chat_id': '123456', 'hh_message_id': '234567', 'force_send': True}),
                'manual': ('_start_manual_ai_apply', {'token': 'synthetic'}),
            }[kind]
            result = await inspect.unwrap(getattr(bot, name))(bot, 42, principal, profile_name='qa', **kwargs)
        for worker in workers:
            if worker.task:
                await worker.task
        return result
    result = asyncio.run(run())
    actual = normalize({'return': result, 'texts': [[c.args, c.kwargs] for c in bot._send_text.call_args_list],
                        'documents': [[c.args, c.kwargs] for c in bot._send_document.call_args_list],
                        'deleted': [[c.args, c.kwargs] for c in bot._delete_message.call_args_list],
                        'audit': audit, 'cleared': cleared, 'authorized': authorized, 'captures': captures},
                       bot._profile('qa').home_dir)
    frozen(f'runner:{kind}:{outcome}', actual)


DIRECTION_CASES = [('/directions', ''), ('/direction_summary', ''), ('/direction_cancel', ''),
                   ('/direction_save', ''), ('/direction_single', ''), ('/direction_ab', ''),
                   ('/direction_edit', ''), ('/search_edit', ''),
                   *[('/direction_edit', field) for field in ('title', 'queries', 'filter', 'relevant', 'exclude',
                       'salary', 'positioning', 'goal', 'city', 'format', 'search_profiles',
                       'work_formats', 'fly_allowed', 'work_cities', 'cover_letter_fallback', 'invalid')],
                   ('/direction_suggest', ''), ('/direction_refresh', ''), ('/direction_stats_refresh', ''),
                   ('/direction_unknown', '')]


@pytest.mark.parametrize('active', [False, True])
@pytest.mark.parametrize('command,arg', DIRECTION_CASES)
def test_direction_outcomes(tmp_path, monkeypatch, active, command, arg):
    bot = Harness(tmp_path)
    monkeypatch.setattr('telegram_app.history.token', lambda home: 'synthetic-token')
    home = Path(bot.profiles['client_1'].home_dir)
    monkeypatch.setattr('uuid.uuid4', lambda: type('Id', (), {'hex': '0123456789abcdef0123456789abcdef'})())
    if active:
        value = make_direction(home)
        directions.set_active_direction(home, value['name'])
    async def suggest(**kw):
        return {'title': 'Synthetic direction', 'search_queries': ['Synthetic QA']}
    monkeypatch.setattr('search_query_suggester.suggest_direction', suggest)
    from hh import negotiation_snapshot
    monkeypatch.setattr(negotiation_snapshot, 'refresh_for_chats', lambda *a: [{'complete': True}])
    async def run():
        result = await bot._dispatch_direction(1, USER, command, arg)
        if bot.command and bot.command.task:
            await bot.command.task
        return result
    before = profile_files(home)
    result = asyncio.run(run())
    after = profile_files(home)
    actual = normalize({'return': result, 'sent': bot.sent, 'sessions': bot._direction_sessions(),
                        'auth': bot.auth_calls, 'downloads': bot.downloads,
                        'writes': {p: after.get(p) for p in before.keys() | after.keys() if before.get(p) != after.get(p)}}, home)
    actual = normalize(actual, tmp_path)
    frozen(f'direction:{active}:{command}:{arg}', actual)
