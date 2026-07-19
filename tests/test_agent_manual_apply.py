import asyncio

import agent
import manual_apply_queue
import notifier


class FakeHHClient:
    async def start(self):
        return None

    async def is_logged_in(self):
        return True

    async def stop(self):
        return None


def test_manual_ai_apply_does_not_submit_empty_cover(tmp_path, monkeypatch):
    queue_file = tmp_path / "manual_apply_queue.json"
    monkeypatch.setattr(manual_apply_queue.config, "MANUAL_APPLY_QUEUE_FILE", str(queue_file), raising=False)

    item = manual_apply_queue.create_candidate(
        {
            "id": "123",
            "source": "hh",
            "source_label": "hh.ru",
            "title": "Manual QA Engineer",
            "company": "Acme",
            "url": "https://hh.ru/vacancy/123",
        },
        {"score": 55, "reason": "yellow", "guard_flags": ["below_auto_apply_threshold"]},
        details="Подробное описание вакансии",
        profile_name="qa",
    )
    token = item["token"]

    monkeypatch.setattr(agent, "HHClient", FakeHHClient)
    monkeypatch.setattr(agent.hh_guard, "can_auto_apply", lambda: (True, ""))
    monkeypatch.setattr(agent.apply_orchestrator, "get_cover_letter_limit", lambda source: 1000)
    monkeypatch.setattr(agent.analytics, "new_run_id", lambda prefix: "run-1")

    decisions = []

    def fake_record_decision(**kwargs):
        decisions.append(kwargs)

    monkeypatch.setattr(agent.analytics, "record_decision", fake_record_decision)

    async def fake_generate_cover_letter(vacancy, details):
        return "   "

    monkeypatch.setattr(agent, "generate_cover_letter", fake_generate_cover_letter)

    async def fail_dispatch(*args, **kwargs):
        raise AssertionError("dispatch_apply must not run without a cover letter")

    monkeypatch.setattr(agent.apply_orchestrator, "dispatch_apply", fail_dispatch)

    notifications = []

    async def fake_notify_needs_manual(vacancy, score, reason, note="", reply_markup=None):
        notifications.append(note)

    monkeypatch.setattr(agent, "notify_needs_manual", fake_notify_needs_manual)

    result = asyncio.run(agent.do_manual_apply_token(token))

    assert result["ok"] is False
    assert result["no_cover"] is True
    assert "без текста" in result["message"]
    assert manual_apply_queue.get_candidate(token)["status"] == "failed_no_cover"
    assert "без текста" in notifications[0]
    assert decisions[0]["decision"] == agent.DECISION_APPLY_FAILED
    assert "no_cover_letter" in decisions[0]["evaluation"]["guard_flags"]


def test_format_hh_question_answers_for_note_limits_output():
    result = {
        "question_answers": [
            {"question": f"Вопрос {idx}", "answer": f"Ответ {idx}", "best_guess": idx == 2}
            for idx in range(1, 11)
        ]
    }

    note = agent._format_hh_question_answers_for_note(result)

    assert "Анкета HH заполнена:" in note
    assert "1. Вопрос 1 -> Ответ 1" in note
    assert "2. Вопрос 2 -> Ответ 2 [best-guess]" in note
    assert "8. Вопрос 8 -> Ответ 8" in note
    assert "...ещё 2 ответ(ов)" in note
    assert "Вопрос 9" not in note


def test_format_hh_question_answers_marks_skipped_risky_question():
    note = agent._format_hh_question_answers_for_note({
        "question_answers": [
            {
                "question": "Коммерческий опыт AQA?",
                "answer": "Нужно ручное подтверждение",
                "skipped": True,
                "skip_reason": "risky_question",
            }
        ]
    })

    assert "Коммерческий опыт AQA? -> Нужно ручное подтверждение [пропущено | risky_question]" in note


def test_format_autoanswer_notes_for_questionnaire_escapes_html():
    note = (
        "автоответ hh: техническая запись\n\n"
        "Анкета HH заполнена:\n"
        "1. Опыт <API> & SQL -> Есть <да> & Postman"
    )

    formatted = notifier._format_autoanswer_notes(note)

    assert formatted.startswith("Анкета HH заполнена:")
    assert "автоответ hh" not in formatted
    assert "Опыт &lt;API&gt; &amp; SQL" in formatted
    assert "Есть &lt;да&gt; &amp; Postman" in formatted
    assert "<API>" not in formatted
