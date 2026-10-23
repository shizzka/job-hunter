"""Offline acceptance for every mode entry point and stale confirmation buttons."""
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import directions
import profile as profile_mod
import telegram_bot
import telegram_bot_ui as ui
from telegram_app import onboarding
from tests.test_telegram_directions import make_direction, run_async


@pytest.fixture
def mode_bot(monkeypatch):
    for name in ("qa", "other"):
        env = Path(profile_mod.profile_env_path(name))
        env.parent.mkdir(parents=True)
        env.write_bytes(b"# synthetic settings\nHH_APPLICATION_MODE=confirm\n")
    bot = telegram_bot.TelegramBot("qa")
    bot.selected = "qa"
    principal = {"user_id": 42, "role": ui.ROLE_ADMIN, "profile": "qa"}
    monkeypatch.setattr(bot, "_selected_profile", lambda principal: bot.selected)
    monkeypatch.setattr(bot, "_profile_names", lambda: ["qa", "other"])
    monkeypatch.setattr(bot, "_active_command", lambda name: None)
    monkeypatch.setattr(bot, "_has_active_command", lambda name: False)
    monkeypatch.setattr(bot, "_reload_profile_daemon", Mock(return_value=False))
    monkeypatch.setattr(bot, "_send_text", AsyncMock(return_value={}))
    monkeypatch.setattr(bot, "_answer_callback_query", AsyncMock())
    monkeypatch.setattr(telegram_bot.telegram_access, "resolve_user", lambda user_id: principal)
    return bot, principal


def add_direction(bot, *, name="qa", mode="confirm"):
    home = bot._profile(bot.selected).home_dir
    value = make_direction(home, name)
    value["application_mode"] = mode
    directions.save_direction(home, value, texts={})
    directions.set_active_direction(home, name)


def settings_bytes(bot):
    # Include the legacy profile setting and direction settings, with exact bytes.
    home = Path(bot._profile(bot.selected).home_dir)
    return {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()}


async def callback(bot, data):
    await bot._handle_callback_query({"id": "mode-choice", "from": {"id": 42}, "data": data,
        "message": {"message_id": 10, "chat": {"id": 42, "type": "private"}}})


async def choose(bot, principal, entry, mode):
    if entry == "onboarding":
        await callback(bot, "gs:" + mode)
    elif entry == "command":
        await bot._dispatch(42, principal, "/search_application_" + mode, "")
    else:
        label = {"auto": ui.BUTTON_APPLICATION_AUTO, "confirm": ui.BUTTON_APPLICATION_CONFIRM,
                 "preview": ui.BUTTON_APPLICATION_PREVIEW}[mode]
        if entry == "marked_menu":
            label = "✅ " + label
        command, arg = ui._resolve_message_command(label, principal["role"])
        await bot._dispatch(42, principal, command, arg)


def confirmation_buttons(bot):
    call = bot._send_text.call_args
    assert "Бот будет сам откликаться на подходящие вакансии" in call.args[1]
    assert call.args[1].endswith(". Включить?")
    buttons = call.kwargs["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["Да, включить", "Нет"]
    return [b["callback_data"] for b in buttons]


@pytest.mark.parametrize("with_direction", [True, False])
@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
@pytest.mark.parametrize("entry", ["onboarding", "menu", "marked_menu", "command"])
@pytest.mark.parametrize("answer", ["yes", "no"])
@run_async
async def test_auto_requires_confirmation_from_every_entry(mode_bot, with_direction, role, entry, answer):
    bot, principal = mode_bot
    principal["role"] = role
    if with_direction:
        add_direction(bot)
    before = settings_bytes(bot)
    await choose(bot, principal, entry, "auto")
    yes, no = confirmation_buttons(bot)
    assert settings_bytes(bot) == before
    bot._reload_profile_daemon.assert_not_called()
    if with_direction:
        assert "направления «QA»" in bot._send_text.call_args.args[1]
    await callback(bot, yes if answer == "yes" else no)
    assert bot._profile("qa").hh.application_mode == ("auto" if answer == "yes" else "confirm")
    if answer == "no":
        assert settings_bytes(bot) == before
        bot._reload_profile_daemon.assert_not_called()
    else:
        bot._reload_profile_daemon.assert_called_once_with("qa")
        labels = [b["text"] for row in bot._send_text.call_args.kwargs["reply_markup"]["keyboard"] for b in row]
        assert "✅ " + ui.BUTTON_APPLICATION_AUTO in labels
    # A refused or already consumed confirmation never turns into a new save.
    after = settings_bytes(bot)
    await callback(bot, yes)
    assert settings_bytes(bot) == after
    assert "устарела" in bot._send_text.call_args.args[1]


@pytest.mark.parametrize("with_direction", [True, False])
@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
@pytest.mark.parametrize("entry", ["onboarding", "menu", "marked_menu", "command"])
@run_async
async def test_safe_mode_is_immediate_and_visible(mode_bot, with_direction, role, entry):
    bot, principal = mode_bot
    principal["role"] = role
    if with_direction:
        add_direction(bot, mode="auto")
    else:
        profile_mod.update_profile_env("qa", {"HH_APPLICATION_MODE": "auto"})
    await choose(bot, principal, entry, "confirm")
    assert bot._profile("qa").hh.application_mode == "confirm"
    call = bot._send_text.call_args
    assert "Режим: спрашивать вас перед каждым откликом" in call.args[1]
    labels = [b["text"] for row in call.kwargs["reply_markup"]["keyboard"] for b in row]
    assert "✅ " + ui.BUTTON_APPLICATION_CONFIRM in labels
    assert "✅ " + ui.BUTTON_APPLICATION_AUTO not in labels
    assert 42 not in bot._direction_sessions()


@pytest.mark.parametrize("change", ["safe", "preview", "new_prompt", "profile", "direction", "edit", "legacy_mode"])
@run_async
async def test_old_yes_cannot_enable_auto_after_context_changes(mode_bot, change):
    bot, principal = mode_bot
    if change != "legacy_mode":
        add_direction(bot)
    await choose(bot, principal, "onboarding", "auto")
    yes, _ = confirmation_buttons(bot)
    if change in {"safe", "preview"}:
        await choose(bot, principal, "command", "confirm" if change == "safe" else "preview")
    elif change == "new_prompt":
        await choose(bot, principal, "menu", "auto")
        assert yes != confirmation_buttons(bot)[0]
    elif change == "profile":
        bot.selected = "other"
    elif change == "direction":
        add_direction(bot, name="other")
    elif change == "edit":
        value = directions.active_direction(bot._profile("qa").home_dir)
        value["title"] = "Changed direction"
        directions.save_direction(bot._profile("qa").home_dir, value, texts={})
    else:
        profile_mod.update_profile_env("qa", {"HH_APPLICATION_MODE": "preview"})
    before = settings_bytes(bot)
    await callback(bot, yes)
    assert settings_bytes(bot) == before
    assert bot._profile(bot.selected).hh.application_mode != "auto"
    # Even an old onboarding button must open a fresh question.
    await callback(bot, "gs:auto")
    confirmation_buttons(bot)
    assert settings_bytes(bot) == before


@run_async
async def test_old_draft_save_cannot_accept_auto_question(mode_bot):
    bot, principal = mode_bot
    add_direction(bot)
    await bot._dispatch(42, principal, "/direction_summary", "")
    old_save = "dir:" + bot._direction_sessions()[42]["token"] + ":save"
    await choose(bot, principal, "command", "auto")
    state = bot._direction_sessions()[42]
    before = settings_bytes(bot)
    await callback(bot, old_save)
    await callback(bot, "dir:" + state["token"] + ":save")
    await bot._dispatch(42, principal, "/direction_save", "")
    assert settings_bytes(bot) == before
    assert bot._profile("qa").hh.application_mode == "confirm"


@pytest.mark.parametrize("mode", ["confirm", "auto", "preview"])
@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
def test_current_mode_is_marked_on_each_mode_picker(mode_bot, mode, role):
    bot, principal = mode_bot
    principal["role"] = role
    add_direction(bot, mode=mode)
    markup = bot._menu_reply_markup(principal, menu=ui.MENU_APPLICATION_MODE)
    marked = [b["text"] for row in markup["keyboard"] for b in row if b["text"].startswith("✅ ")]
    assert len(marked) == 1
    assert ui._resolve_message_command(marked[0], principal["role"]) == ("/search_application_" + mode, "")
    _, rows, _ = onboarding.build(hh_ok=True, resumes=[], directions=[], active=None, mode=mode, has_search=False)
    for label, action in (b for row in rows for b in row if b[1] in {"auto", "confirm"}):
        assert label.startswith("✅ ") == (action == mode)


@pytest.mark.parametrize("mode", ["confirm", "preview", "auto"])
@run_async
async def test_mode_saved_but_restart_failure_is_explicit(mode_bot, mode):
    bot, principal = mode_bot
    add_direction(bot, mode="preview" if mode == "auto" else "auto")
    bot._reload_profile_daemon.side_effect = RuntimeError("synthetic restart failure")
    await choose(bot, principal, "command", mode)
    if mode == "auto":
        yes, _ = confirmation_buttons(bot)
        await callback(bot, yes)
    assert bot._profile("qa").hh.application_mode == mode
    bot._reload_profile_daemon.assert_called_once_with("qa")
    call = bot._send_text.call_args
    assert call.args[1].startswith("⚠️")
    assert "Настройки сохранены, но повтор не перезапустился" in call.args[1]
    assert "synthetic restart failure" in call.args[1]
    assert "Новый режим может ещё не действовать" in call.args[1]
    assert call.kwargs["reply_markup"] == ui.build_reply_markup(
        principal["role"], menu=ui.MENU_APPLICATION_MODE, application_mode=mode)


@pytest.mark.parametrize("with_direction", [True, False])
@pytest.mark.parametrize("mode", ["confirm", "preview", "auto"])
@pytest.mark.parametrize("outcome", ["denied", "process", "task"])
@run_async
async def test_stop_from_mode_menu_preserves_selection_and_controls(mode_bot, monkeypatch, with_direction, mode, outcome):
    bot, principal = mode_bot
    principal["role"] = ui.ROLE_USER
    if with_direction:
        add_direction(bot, mode=mode)
    else:
        profile_mod.update_profile_env("qa", {"HH_APPLICATION_MODE": mode})
    command = telegram_bot.ActiveCommandState(label="search", profile_name="qa",
        owner_user_id=7 if outcome == "denied" else 42, started_at=0,
        subprocess_pid=123 if outcome == "process" else 0)
    monkeypatch.setattr(bot, "_active_command", lambda name: command)
    cancel = Mock(return_value={"signal": "SIGTERM"} if outcome == "process" else None)
    monkeypatch.setattr(bot, "_request_active_command_cancel", cancel)
    await bot._dispatch(42, principal, "/search_application_mode", "")
    assert bot._send_text.call_args.kwargs["reply_markup"] == ui.build_reply_markup(
        ui.ROLE_USER, menu=ui.MENU_APPLICATION_MODE, application_mode=mode,
        active=True, can_stop=outcome != "denied")
    await bot._dispatch(42, principal, "/cancel_active", "")
    assert bot._send_text.call_args.kwargs["reply_markup"] == ui.build_reply_markup(
        ui.ROLE_USER, menu=ui.MENU_APPLICATION_MODE, application_mode=mode,
        active=True, can_stop=False)
    if outcome == "denied":
        cancel.assert_not_called()
    else:
        cancel.assert_called_once_with(command)
