"""Offline acceptance for the search list, cards, and per-search editors."""
import copy
from pathlib import Path

import pytest

import directions
from tests.test_telegram_directions import Harness, USER, make_direction, run_async


@pytest.fixture
def search_bot(tmp_path):
    bot = Harness(tmp_path)
    home = bot.profiles['client_1'].home_dir
    make_direction(home, 'active')
    other = make_direction(home, 'other')
    other.update(application_mode='confirm', matcher_threshold=60,
                 work_conditions={'allowed_formats': ['remote'], 'allow_fly_in_fly_out': False, 'allowed_cities': []})
    directions.save_direction(home, other, texts={})
    directions.set_active_direction(home, 'active')
    return bot, home


def buttons(bot):
    return next(kwargs['reply_markup']['inline_keyboard'] for _, _, kwargs in reversed(bot.sent)
                if 'inline_keyboard' in kwargs.get('reply_markup', {}))


def action(bot, suffix):
    return next(b['callback_data'] for row in buttons(bot) for b in row
                if b['callback_data'].split(':', 2)[2] == suffix)


async def open_other(bot):
    await bot._dispatch_direction(1, USER, '/directions', '')
    await bot._direction_callback(1, USER, action(bot, 'choose:1'))


@run_async
async def test_search_list_has_human_summary_and_card_for_inactive_search(search_bot):
    bot, home = search_bot
    await bot._dispatch_direction(1, USER, '/directions', '')
    label = bot.sent[-1][1]
    assert 'OTHER' in label
    assert '🏠 Только удалённо · ⚖️ сбалансированно · ✋ подтверждать' in label
    assert '60' not in label
    assert any(b['text'] == '➕ Новый поиск' for row in buttons(bot) for b in row)
    await bot._direction_callback(1, USER, action(bot, 'choose:1'))
    labels = [button['text'] for row in buttons(bot) for button in row]
    assert labels == ['🔎 Что искать', '📍 Где и как работать', '📄 Резюме',
                      '🛡 Режим откликов', '⚖️ Строгость отбора', '◀ Назад', '🗑 Удалить поиск…']
    assert [len(row) for row in buttons(bot)] == [2, 2, 1, 1, 1]
    assert '60+' in bot.sent[-1][1]
    assert directions.active_direction(home)['name'] == 'active'
    assert not bot._direction_sessions()[1].get('activate')


@pytest.mark.parametrize('value', [75, 60, 45])
@run_async
async def test_strictness_preset_is_confirmed_draft_for_selected_search(search_bot, value):
    bot, home = search_bot
    await open_other(bot)
    await bot._direction_callback(1, USER, action(bot, 'threshold'))
    old = action(bot, f'thresholdset:{value}')
    await bot._direction_callback(1, USER, old)
    assert bot._direction_sessions()[1]['draft']['matcher_threshold'] == value
    assert directions.load_direction(home, 'other')['matcher_threshold'] == 60
    await bot._direction_callback(1, USER, old)
    assert 'устарела' in bot.sent[-1][1]
    await bot._dispatch_direction(1, USER, '/direction_save', '')
    assert directions.load_direction(home, 'other')['matcher_threshold'] == value
    assert directions.active_direction(home)['name'] == 'active'


@pytest.mark.parametrize('value', ['-1', '101', '1.5', 'много'])
@run_async
async def test_custom_threshold_refuses_invalid_input_without_mutation(search_bot, value):
    bot, home = search_bot
    await open_other(bot)
    await bot._direction_callback(1, USER, action(bot, 'threshold'))
    await bot._direction_callback(1, USER, action(bot, 'thresholdcustom'))
    before = copy.deepcopy(bot._direction_sessions()[1]['draft'])
    await bot._accept_direction_input(1, USER, value)
    assert bot._direction_sessions()[1]['draft'] == before
    assert '0–100' in bot.sent[-1][1]
    await bot._accept_direction_input(1, USER, '0')
    await bot._dispatch_direction(1, USER, '/direction_save', '')
    assert directions.load_direction(home, 'other')['matcher_threshold'] == 0


@pytest.mark.parametrize('answer', ['modeyes', 'modeno'])
@run_async
async def test_selected_search_auto_mode_requires_bound_confirmation(search_bot, answer):
    bot, home = search_bot
    await open_other(bot)
    await bot._direction_callback(1, USER, action(bot, 'applicationmode'))
    await bot._direction_callback(1, USER, action(bot, 'modeset:auto'))
    yes = action(bot, 'modeyes')
    no = action(bot, 'modeno')
    assert directions.load_direction(home, 'other')['application_mode'] == 'confirm'
    await bot._direction_callback(1, USER, yes if answer == 'modeyes' else no)
    assert bot._direction_sessions()[1]['draft']['application_mode'] == ('auto' if answer == 'modeyes' else 'confirm')
    await bot._direction_callback(1, USER, yes)
    assert 'устарела' in bot.sent[-1][1]
    assert directions.load_direction(home, 'other')['application_mode'] == 'confirm'


@run_async
async def test_inactive_search_change_invalidates_editor_and_save(search_bot):
    bot, home = search_bot
    await open_other(bot)
    card = action(bot, 'threshold')
    changed = directions.load_direction(home, 'other')
    changed['title'] = 'CHANGED'
    directions.save_direction(home, changed, texts={})
    await bot._direction_callback(1, USER, card)
    assert 'устарела' in bot.sent[-1][1]
    await bot._dispatch_direction(1, USER, '/direction_save', '')
    assert directions.load_direction(home, 'other')['title'] == 'CHANGED'


@run_async
async def test_work_editor_has_one_format_source_and_delete_requires_confirmation(search_bot):
    bot, home = search_bot
    await open_other(bot)
    await bot._direction_callback(1, USER, action(bot, 'editmenu'))
    labels = [b['text'] for row in buttons(bot) for b in row]
    assert not any('hh.ru' in label or 'фильтра' in label for label in labels)
    await bot._direction_callback(1, USER, action(bot, 'back'))
    await bot._direction_callback(1, USER, action(bot, 'delete'))
    assert directions.load_direction(home, 'other')
    yes = action(bot, 'deleteyes')
    await bot._direction_callback(1, USER, action(bot, 'back'))
    await bot._direction_callback(1, USER, yes)
    assert directions.load_direction(home, 'other')
    await bot._direction_callback(1, USER, action(bot, 'delete'))
    await bot._direction_callback(1, USER, action(bot, 'deleteyes'))
    assert not (Path(home) / 'directions' / 'other.json').exists()
    assert directions.active_direction(home)['name'] == 'active'


@pytest.mark.parametrize('mode', ['confirm', 'preview'])
@run_async
async def test_safe_card_modes_save_only_selected_search(search_bot, mode):
    bot, home = search_bot
    await open_other(bot)
    active = directions.active_direction(home)
    await bot._direction_callback(1, USER, action(bot, 'applicationmode'))
    await bot._direction_callback(1, USER, action(bot, 'modeset:' + mode))
    assert bot._direction_sessions()[1]['draft']['application_mode'] == mode
    await bot._dispatch_direction(1, USER, '/direction_save', '')
    assert directions.load_direction(home, 'other')['application_mode'] == mode
    assert directions.active_direction(home) == active


@pytest.mark.parametrize('pending', ['threshold', 'mode', 'delete'])
@pytest.mark.parametrize('change', ['target', 'profile', 'active', 'deleted'])
@run_async
async def test_card_mutations_reject_changed_context(search_bot, pending, change):
    bot, home = search_bot
    await open_other(bot)
    if pending == 'threshold':
        await bot._direction_callback(1, USER, action(bot, 'threshold'))
        old = action(bot, 'thresholdset:75')
    elif pending == 'mode':
        await bot._direction_callback(1, USER, action(bot, 'applicationmode'))
        await bot._direction_callback(1, USER, action(bot, 'modeset:auto'))
        old = action(bot, 'modeyes')
    else:
        await bot._direction_callback(1, USER, action(bot, 'delete'))
        old = action(bot, 'deleteyes')
    principal = USER
    if change == 'target':
        value = directions.load_direction(home, 'other')
        value['title'] = 'CHANGED'
        directions.save_direction(home, value, texts={})
    elif change == 'profile':
        principal = {**USER, 'profile': 'client_2'}
    elif change == 'active':
        directions.set_active_direction(home, 'other')
    else:
        value = directions.load_direction(home, 'other')
        directions.delete_direction(home, 'other', expected=value)
    before = {p: p.read_bytes() for p in Path(home).rglob('*') if p.is_file()}
    await bot._direction_callback(1, principal, old)
    assert 'устарела' in bot.sent[-1][1]
    assert {p: p.read_bytes() for p in Path(home).rglob('*') if p.is_file()} == before


@run_async
async def test_custom_threshold_upper_boundary_is_valid(search_bot):
    bot, home = search_bot
    await open_other(bot)
    await bot._direction_callback(1, USER, action(bot, 'threshold'))
    await bot._direction_callback(1, USER, action(bot, 'thresholdcustom'))
    await bot._accept_direction_input(1, USER, '100')
    assert bot._direction_sessions()[1]['draft']['matcher_threshold'] == 100
    await bot._dispatch_direction(1, USER, '/direction_save', '')
    assert directions.load_direction(home, 'other')['matcher_threshold'] == 100


@pytest.mark.parametrize('mode,word,label', [('auto', '🤖 автоматически', '🤖 Откликаться автоматически'),
                                          ('preview', '👀 показывать', '👀 Только показывать')])
@run_async
async def test_inherited_mode_uses_base_profile_and_browsing_keeps_inheritance(search_bot, mode, word, label):
    from types import SimpleNamespace
    bot, home = search_bot
    value = directions.load_direction(home, 'other')
    value.pop('application_mode')
    directions.save_direction(home, value, texts={})
    # The active search has an explicit confirm override; the other inherits.
    profile = bot.profiles['client_1']
    profile.hh.application_mode = 'confirm'
    profile._direction_base = SimpleNamespace(hh=SimpleNamespace(application_mode=mode))
    await bot._dispatch_direction(1, USER, '/directions', '')
    assert word in bot.sent[-1][1]
    await bot._direction_callback(1, USER, action(bot, 'choose:1'))
    assert label in bot.sent[-1][1]
    await bot._direction_callback(1, USER, action(bot, 'applicationmode'))
    assert any(b['text'] == '✓ ' + label for row in buttons(bot) for b in row)
    await bot._direction_callback(1, USER, action(bot, 'modeset:' + mode))
    assert 'application_mode' not in bot._direction_sessions()[1]['draft']
    await bot._direction_callback(1, USER, action(bot, 'threshold'))
    await bot._direction_callback(1, USER, action(bot, 'thresholdset:75'))
    await bot._dispatch_direction(1, USER, '/direction_save', '')
    assert 'application_mode' not in directions.load_direction(home, 'other')


@run_async
async def test_auto_confirmation_rejects_changed_inherited_mode(search_bot):
    from types import SimpleNamespace
    bot, home = search_bot
    profile = bot.profiles['client_1']
    profile._direction_base = SimpleNamespace(hh=SimpleNamespace(application_mode='confirm'))
    await open_other(bot)
    await bot._direction_callback(1, USER, action(bot, 'applicationmode'))
    await bot._direction_callback(1, USER, action(bot, 'modeset:auto'))
    yes = action(bot, 'modeyes')
    profile._direction_base.hh.application_mode = 'preview'
    await bot._direction_callback(1, USER, yes)
    assert 'устарела' in bot.sent[-1][1]
    assert directions.load_direction(home, 'other')['application_mode'] == 'confirm'
