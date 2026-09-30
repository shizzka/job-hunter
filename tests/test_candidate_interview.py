import asyncio
from types import SimpleNamespace

import candidate_interview
import prompt_blocks
import telegram_bot
import telegram_bot_ui as ui


def test_confirmed_facts_are_profile_scoped_and_prompted(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    candidate_interview.add_fact("Linux: только базовый терминал.", topic="инструменты", profile_dir=str(first))

    assert "базовый терминал" in candidate_interview.render_facts(profile_dir=str(first))
    assert candidate_interview.facts(str(second)) == []
    assert "не усиливай уровень" in candidate_interview.prompt_block(profile_dir=str(first))


def test_plan_is_normalized_and_falls_back_without_valid_llm(monkeypatch):
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"profile_hypothesis":"электрик","questions":[{"topic":"допуск","question":"Какая группа?","answer_hint":"Укажите действующую."}]}'))])

    monkeypatch.setattr(candidate_interview, "get_llm_client", lambda: Client())
    plan = asyncio.run(candidate_interview.build_plan("# Электромонтажник", target_role="электрик"))

    assert plan["profile_hypothesis"] == "электрик"
    assert plan["questions"][0]["topic"] == "допуск"


def test_user_fact_is_in_shared_prompt_block(tmp_path, monkeypatch):
    candidate_interview.add_fact("Не заявлять продвинутый Linux.", profile_dir=str(tmp_path))
    monkeypatch.setattr(candidate_interview, "_profile_dir", lambda: str(tmp_path))

    assert "Не заявлять продвинутый Linux" in prompt_blocks.build_facts_block()


def test_telegram_answer_needs_confirmation_before_write(tmp_path, monkeypatch):
    bot = telegram_bot.TelegramBot("qa")
    principal = {"user_id": 77, "role": ui.ROLE_USER, "profile": "qa"}
    messages = []
    profile_dir = tmp_path / "qa"
    monkeypatch.setattr(bot, "_selected_profile", lambda _: "qa")
    monkeypatch.setattr(bot, "_candidate_profile_dir", lambda _: str(profile_dir))
    monkeypatch.setattr(bot, "_menu_reply_markup", lambda *args, **kwargs: {})

    async def send_text(*args, **kwargs):
        messages.append(args[1])

    monkeypatch.setattr(bot, "_send_text", send_text)
    bot._set_candidate_state(77, {"profile_name": "qa", "kind": "interview", "mode": "answer", "questions": [{"topic": "Linux", "question": "Какой уровень?"}], "index": 0})

    assert asyncio.run(bot._accept_candidate_interview_input(77, principal, "Только базовый терминал."))
    assert candidate_interview.facts(str(profile_dir)) == []
    assert "подтверждённый факт" in messages[-1]

    asyncio.run(bot._dispatch(77, principal, "/candidate_save", ""))
    assert candidate_interview.facts(str(profile_dir))[0]["text"] == "Только базовый терминал."


def test_candidate_menu_is_available_to_regular_user():
    markup = ui.build_reply_markup(ui.ROLE_USER, menu=ui.MENU_CANDIDATE)
    assert ui.BUTTON_CANDIDATE_FACTS in str(markup)
    assert ui.BUTTON_CANDIDATE_PROFILE in str(ui.build_reply_markup(ui.ROLE_USER))
