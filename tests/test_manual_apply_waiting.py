"""Prod 19.10: an employer question looked like «❌ manual AI apply завершён с ошибкой» and repeated taps
stacked identical question cards."""
import question_memory as qm
from telegram_bot_ui import format_command_result


def test_waiting_outcomes_are_not_errors():
    for line in ("❓ Работодатель задал вопрос — ответь в карточке «Работодатель спрашивает», и я откликнусь сам.",
                 "⏸ Письмо не прошло проверку фактов, получился шаблон — без твоего решения не отправляю."):
        text = format_command_result("manual AI apply", {"ok": True, "returncode": 0, "stdout": "log\n" + line + "\n"})
        assert text.startswith("⏸ Отклик ждёт твоего решения") and line in text and "ошибк" not in text.split("\n")[0]


def test_same_question_for_same_vacancy_is_found(tmp_path):
    qid = qm.create_pending(str(tmp_path), question="Каким инструментом пользуетесь чаще всего? Почему?",
                            control="textarea", options=[], token="t1", profile_name="qa",
                            vacancy_title="Junior QA", company="A", url="https://hh.ru/vacancy/1")
    assert qm.find_pending(str(tmp_path), question="каким инструментом пользуетесь чаще всего почему",
                           url="https://hh.ru/vacancy/1") == qid
    assert qm.find_pending(str(tmp_path), question="Каким инструментом пользуетесь чаще всего?", url="https://hh.ru/vacancy/2") == ""


def test_repeated_question_does_not_stack_cards(tmp_path, monkeypatch):
    import asyncio
    import agent
    import manual_apply_queue
    monkeypatch.setattr(agent.config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(manual_apply_queue, "_queue_path", lambda profile_name=None: tmp_path / "q.json")
    cards = []

    async def send(text, reply_markup=None, **kwargs):
        cards.append(text)
        return True

    monkeypatch.setattr(agent.notifier, "send_message_with_markup", send)
    vacancy = {"id": "1", "title": "Junior QA", "company": "A", "url": "https://hh.ru/vacancy/1", "source": "hh"}
    pending = {"question_text": "Каким инструментом пользуетесь чаще всего?", "control": "textarea", "options": []}
    for _ in range(2):
        assert asyncio.run(agent._notify_employer_question(vacancy, {"score": 60}, "", 60, "ok", pending,
                                                           profile_name="qa"))
    assert len(cards) == 1
    import json
    assert len(json.loads((tmp_path / "q.json").read_text())["items"]) == 1
