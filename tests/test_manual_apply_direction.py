import asyncio
from types import SimpleNamespace

import directions
import manual_apply_queue
import telegram_bot
from tests.test_telegram_directions import make_direction


def setup(tmp_path, monkeypatch):
    home = tmp_path / "client_1"
    home.mkdir()
    make_direction(str(home), "electric")
    make_direction(str(home), "qa")
    directions.set_active_direction(str(home), "qa")
    bot = telegram_bot.TelegramBot("qa")
    sent, runs = [], []
    monkeypatch.setattr(bot, "_profile", lambda name: SimpleNamespace(home_dir=str(home)))
    monkeypatch.setattr(bot, "_has_active_command", lambda name: False)
    monkeypatch.setattr(bot, "_menu_reply_markup", lambda principal, menu=None: None)
    monkeypatch.setattr(bot, "_mark_active_command", lambda **kw: SimpleNamespace(task=None))
    monkeypatch.setattr(bot, "_clear_active_command", lambda name: None)
    monkeypatch.setattr(manual_apply_queue, "get_candidate", lambda token, profile_name=None: {
        "vacancy": {"id": "1", "_direction_selection": {"direction": "electric"}}})

    async def send_text(chat_id, text, **kwargs):
        sent.append((text, kwargs.get("reply_markup")))
        return {}

    async def run_capture(command, argv, timeout):
        runs.append((directions.active_direction(str(home))["name"], argv))
        return {"returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bot, "_send_text", send_text)
    monkeypatch.setattr(bot, "_run_active_command_capture", run_capture)
    return bot, str(home), sent, runs


def test_card_from_another_direction_applies_in_it_without_asking(tmp_path, monkeypatch):
    """A tap is the decision — also when it comes from the command queue."""
    bot, home, sent, runs = setup(tmp_path, monkeypatch)

    async def run():
        await bot._start_manual_ai_apply(1, {"user_id": 1, "role": "admin"}, profile_name="client_1", token="t")
        await asyncio.sleep(0.05)
        for task in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            await task

    asyncio.run(run())
    assert runs and runs[0][0] == "electric"
    assert "Карточка из направления «ELECTRIC»" in sent[0][0]
    assert directions.active_direction(home)["name"] == "qa"


def test_override_switches_for_the_apply_and_restores(tmp_path, monkeypatch):
    bot, home, sent, runs = setup(tmp_path, monkeypatch)

    async def run():
        await bot._start_manual_ai_apply(1, {"user_id": 1, "role": "admin"}, profile_name="client_1", token="t",
                                         direction_override=True)
        await asyncio.sleep(0.05)
        for task in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            await task

    asyncio.run(run())
    assert runs and runs[0][0] == "electric"
    assert directions.active_direction(home)["name"] == "qa"
