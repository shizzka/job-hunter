"""B13 in the bot: refuse another profile's chat; switch direction for this profile's other direction."""
import asyncio
import json
from types import SimpleNamespace

import chat_context
import directions
import telegram_bot
from tests.test_telegram_directions import make_direction


def setup(tmp_path, monkeypatch, resume_id):
    home = tmp_path / "profiles" / "client_1"
    home.mkdir(parents=True)
    make_direction(str(home), "electric")
    make_direction(str(home), "qa")
    directions.set_active_direction(str(home), "electric")
    # make_direction gives numeric ids 1, 2 to both directions; make qa's unique
    qa_file = home / "directions" / "qa.json"
    value = json.loads(qa_file.read_text())
    for i, variant in enumerate(value["variants"]):
        variant["hh_resume_numeric_id"] = 500 + i
    qa_file.write_text(json.dumps(value))
    (home / "negotiation_snapshots.jsonl").write_text(json.dumps({"chatId": 77, "resumeId": resume_id}) + "\n")
    bot = telegram_bot.TelegramBot("qa")
    sent, runs = [], []
    monkeypatch.setattr(bot, "_profile", lambda name: SimpleNamespace(home_dir=str(home), hh=SimpleNamespace(cookies_file="")))
    monkeypatch.setattr(bot, "_has_active_command", lambda name: False)
    monkeypatch.setattr(bot, "_menu_reply_markup", lambda principal, menu=None: None)
    monkeypatch.setattr(bot, "_mark_active_command", lambda **kw: SimpleNamespace(task=None))
    monkeypatch.setattr(bot, "_clear_active_command", lambda name: None)
    monkeypatch.setattr(bot, "_append_chat_ai_audit_event", lambda *a, **k: None)

    async def authorize(*args, **kwargs):
        return True

    async def send_text(chat_id, text, **kwargs):
        sent.append(text)
        return {}

    async def run_capture(command, argv, timeout):
        runs.append(directions.active_direction(str(home))["name"])
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(bot, "_authorize_ai_usage", authorize)
    monkeypatch.setattr(bot, "_send_text", send_text)
    monkeypatch.setattr(bot, "_send_text_safely", send_text)
    monkeypatch.setattr(bot, "_run_active_command_capture", run_capture)
    return bot, str(home), sent, runs


def drive(bot):
    async def run():
        await bot._start_chat_ai_reply(1, {"user_id": 1, "role": "admin"}, profile_name="client_1",
                                       hh_chat_id="77", hh_message_id="9")
        await asyncio.sleep(0.05)
        for task in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            await task
    asyncio.run(run())


def test_chat_of_another_resume_is_refused_without_running(tmp_path, monkeypatch):
    bot, home, sent, runs = setup(tmp_path, monkeypatch, resume_id=999)
    drive(bot)
    assert runs == [] and any("Ответ не готовлю" in text for text in sent)


def test_chat_of_this_profiles_other_direction_switches_and_restores(tmp_path, monkeypatch):
    bot, home, sent, runs = setup(tmp_path, monkeypatch, resume_id=500)
    drive(bot)
    assert runs == ["qa"] and directions.active_direction(home)["name"] == "electric"
