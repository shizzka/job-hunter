"""Reply ownership and version-bound Telegram approvals, fake transports only."""
import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import google_form_filler as gforms
from google_forms import drafts
from google_forms.workflow import FormWorkflow
from state_store.bot_state import BotStateStore
from tests.test_google_form_drafts import FakeBot
from tests.test_async_form_workflow import TOKEN, preview
from runtime_context import RuntimePaths


class TransactionBot(FakeBot):
    def _load_state(self):
        return BotStateStore(Path(self.home) / 'test_bot_state.json').load()

    def _update_state(self, mutator):
        return BotStateStore(Path(self.home) / 'test_bot_state.json').update(mutator)


@pytest.fixture
def bot(tmp_path):
    FormWorkflow(str(tmp_path)).repository.remember(TOKEN, preview(), trim_expired=False)
    bot = TransactionBot(str(tmp_path))
    bot._start_google_form_command = AsyncMock()
    return bot


PRINCIPAL = {'user_id': 7, 'role': 'admin'}


def pending(prompt=101):
    return {'profile': 'qa', 'token': TOKEN, 'index': 0, 'prompt_id': prompt, 'created_at': time.time()}


def install_pending(bot):
    item = pending()
    bot._update_state(lambda state: state.update({'form_pending': {'7:7': item, '8:8': pending(999)}, 'last_update_id': 40}))
    return item


def test_button_approval_is_bound_to_displayed_answers(bot):
    asyncio.run(bot._show_form(7, PRINCIPAL, 'qa', TOKEN))
    buttons = [b for row in bot.sent[-1][2]['reply_markup']['inline_keyboard'] for b in row]
    data = next(b['callback_data'] for b in buttons if ':send:' in b.get('callback_data', ''))
    assert len(data.encode()) <= 64
    drafts.save_answer(bot.home, TOKEN, 0, 'Edited after display', 7)
    with pytest.raises(ValueError, match='измен'):
        asyncio.run(bot._form_callback(7, PRINCIPAL, data))
    bot._start_google_form_command.assert_not_awaited()


def test_valid_button_passes_full_revision_to_subprocess(bot):
    _, _, version = FormWorkflow(bot.home).capture(TOKEN)
    asyncio.run(bot._form_callback(7, PRINCIPAL, f'gf:send:qa:{TOKEN}:{version[:12]}'))
    argv = bot._start_google_form_command.await_args.kwargs['argv']
    assert argv[-2:] == ['--google-form-approval-revision', version]


def test_old_unversioned_button_cannot_send(bot):
    with pytest.raises(ValueError, match='Старая кнопка'):
        asyncio.run(bot._form_callback(7, PRINCIPAL, f'gf:send:qa:{TOKEN}:0'))
    bot._start_google_form_command.assert_not_awaited()


def test_queued_approval_cannot_adopt_later_answers(bot, monkeypatch):
    from commands import google_forms as commands
    _, _, version = FormWorkflow(bot.home).capture(TOKEN)
    drafts.save_answer(bot.home, TOKEN, 0, 'Edited before command start', 7)
    monkeypatch.setattr(gforms, '_runtime_paths', lambda: RuntimePaths(bot.home, bot.home, bot.home))
    constructor = AsyncMock()
    monkeypatch.setattr(commands, 'HHClient', constructor)
    with pytest.raises(ValueError, match='измен'):
        asyncio.run(commands.recheck(TOKEN, profile_name='qa', submit_after=True, approval_revision=version))
    constructor.assert_not_called()


def test_unversioned_recheck_submit_cannot_use_fresh_answers_as_old_approval(bot, monkeypatch):
    from commands import google_forms as commands
    monkeypatch.setattr(gforms, '_runtime_paths', lambda: RuntimePaths(bot.home, bot.home, bot.home))
    constructor = AsyncMock()
    monkeypatch.setattr(commands, 'HHClient', constructor)
    with pytest.raises(ValueError, match='подтверждения версии'):
        asyncio.run(commands.recheck(TOKEN, profile_name='qa', submit_after=True))
    constructor.assert_not_called()


@pytest.mark.parametrize('mode', ['answer', 'cancel', 'expired'])
def test_old_pending_action_cannot_clear_or_edit_newer_prompt(bot, mode):
    old = install_pending(bot)
    if mode == 'expired':
        old['created_at'] = time.time() - 90000
        bot._update_state(lambda state: state['form_pending'].update({'7:7': old}))
    newer = pending(202)
    original = bot._update_state
    def raced(mutator):
        original(lambda state: state['form_pending'].update({'7:7': newer}))
        return original(mutator)
    bot._update_state = raced
    message = {'text': '/cancel_form' if mode == 'cancel' else 'Synthetic answer',
               'reply_to_message': {'message_id': 101}}
    asyncio.run(bot._accept_form_answer(7, PRINCIPAL, message))
    state = bot._load_state()
    assert state['form_pending']['7:7'] == newer
    assert '8:8' in state['form_pending'] and state['last_update_id'] == 40
    assert not drafts.manual_answers(bot.home, TOKEN)


@pytest.mark.parametrize('error', [RuntimeError, asyncio.CancelledError])
def test_prompt_delivery_error_cleans_only_own_unbound_request(bot, error):
    install_pending(bot)
    bot._send_text = AsyncMock(side_effect=error('Synthetic delivery failure'))
    with pytest.raises(error):
        asyncio.run(bot._ask_form_field(7, PRINCIPAL, 'qa', TOKEN, 0))
    state = bot._load_state()
    assert '7:7' not in state['form_pending'] and '8:8' in state['form_pending']
    assert state['last_update_id'] == 40


def test_late_failed_delivery_keeps_new_request(bot):
    async def run():
        entered, finish = asyncio.Event(), asyncio.Event()
        calls = 0
        async def send(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await finish.wait()
                raise RuntimeError('Synthetic old request failure')
            return {'message_id': 202}
        bot._send_text = send
        first = asyncio.create_task(bot._ask_form_field(7, PRINCIPAL, 'qa', TOKEN, 0))
        await entered.wait()
        await bot._ask_form_field(7, PRINCIPAL, 'qa', TOKEN, 0)
        finish.set()
        with pytest.raises(RuntimeError):
            await first
    asyncio.run(run())
    assert bot._load_state()['form_pending']['7:7']['prompt_id'] == 202


@pytest.mark.parametrize('value', [[], {'bad': None}, {'bad': {}}, {'bad': {**pending(), 'prompt_id': True}},
                                 {'bad': {**pending(), 'created_at': float('nan')}}])
def test_corrupt_pending_schema_preserved_and_blocks_mutations(bot, value):
    import json
    path = Path(bot.home) / 'test_bot_state.json'
    content = json.dumps({'form_pending': value}).encode()
    path.write_bytes(content)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='restore'):
            bot._update_state(lambda state: state.update(last_update_id=99))
        assert path.read_bytes() == content


def test_pending_methods_do_not_use_whole_snapshot_save(bot):
    def forbidden(*args, **kwargs):
        raise AssertionError('No whole snapshot save')
    bot._save_state = forbidden
    asyncio.run(bot._ask_form_field(7, PRINCIPAL, 'qa', TOKEN, 0))
    prompt = bot._load_state()['form_pending']['7:7']['prompt_id']
    assert asyncio.run(bot._accept_form_answer(7, PRINCIPAL, {'text': 'Reviewed', 'reply_to_message': {'message_id': prompt}}))
    assert not bot._load_state()['form_pending']
