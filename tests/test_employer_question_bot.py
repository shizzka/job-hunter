import asyncio
from types import SimpleNamespace

import question_memory as qm
import telegram_bot


def make_bot(tmp_path, monkeypatch, role=telegram_bot.ROLE_USER, selected="client_1"):
    bot = telegram_bot.TelegramBot("qa")
    sent, started, state = [], [], {}
    monkeypatch.setattr(bot, "_profile_names", lambda: ["client_1", "client_2"])
    monkeypatch.setattr(bot, "_profile", lambda name: SimpleNamespace(home_dir=str(tmp_path / name)))
    monkeypatch.setattr(bot, "_selected_profile", lambda principal: selected)

    async def send_text(chat_id, text, **kwargs):
        sent.append(text)

    async def start(chat_id, principal, *, profile_name, token):
        started.append((profile_name, token))

    monkeypatch.setattr(bot, "_send_text", send_text)
    monkeypatch.setattr(bot, "_start_manual_ai_apply", start)
    monkeypatch.setattr(bot, "_search_settings_state", lambda uid: dict(state))
    monkeypatch.setattr(bot, "_set_search_settings_state", lambda uid, **f: state.update(f))
    monkeypatch.setattr(bot, "_clear_search_settings_state", lambda uid, *keys: [state.pop(k, None) for k in keys])
    return bot, sent, started, {"user_id": 1, "role": role}


def test_choice_answer_is_remembered_and_application_retried(tmp_path, monkeypatch):
    bot, sent, started, principal = make_bot(tmp_path, monkeypatch)
    home = str(tmp_path / "client_1")
    qid = qm.create_pending(home, question="Есть военный билет?", control="radio", options=["Да", "Нет"],
                            token="tok1", profile_name="client_1", vacancy_title="QA")
    asyncio.run(bot._answer_employer_question(1, principal, "client_1", qid, "0"))
    assert qm.lookup(home, "есть военный билет")["labels"] == ["Да"]
    assert started == [("client_1", "tok1")] and qm.peek_pending(home, qid) is None


def test_text_answer_flow(tmp_path, monkeypatch):
    bot, sent, started, principal = make_bot(tmp_path, monkeypatch)
    home = str(tmp_path / "client_1")
    qid = qm.create_pending(home, question="Почему мы?", control="textarea", options=[], token="t2", profile_name="client_1")
    asyncio.run(bot._answer_employer_question(1, principal, "client_1", qid, "text"))
    assert "Пришлите ответ" in sent[-1]
    assert asyncio.run(bot._accept_employer_question_text(1, principal, "Нравится продукт"))
    assert qm.lookup(home, "почему мы")["answer"] == "Нравится продукт" and started == [("client_1", "t2")]


def test_client_cannot_answer_for_another_profile(tmp_path, monkeypatch):
    bot, sent, started, principal = make_bot(tmp_path, monkeypatch)
    home = str(tmp_path / "client_2")
    qid = qm.create_pending(home, question="Q?", control="radio", options=["A"], token="x", profile_name="client_2")
    asyncio.run(bot._answer_employer_question(1, principal, "client_2", qid, "0"))
    assert started == [] and qm.peek_pending(home, qid) is not None


def test_answer_keeps_pending_owner_after_active_direction_changes(tmp_path, monkeypatch):
    bot, _, started, principal = make_bot(tmp_path, monkeypatch)
    home = str(tmp_path / "client_1")
    owner = {"direction": "electric", "resume_id": "electric-resume"}
    qid = qm.create_pending(home, question="Сколько лет опыта?", control="radio", options=["Пять лет"],
                            token="t", profile_name="client_1", **owner)
    monkeypatch.setattr(telegram_bot.config, "ACTIVE_DIRECTION", {"name": "qa"}, raising=False)
    asyncio.run(bot._answer_employer_question(1, principal, "client_1", qid, "0"))
    assert qm.lookup(home, "Сколько лет опыта?", **owner)["answer"] == "Пять лет"
    assert qm.lookup(home, "Сколько лет опыта?", direction="qa", resume_id="qa-resume") is None
    assert started == [("client_1", "t")]
