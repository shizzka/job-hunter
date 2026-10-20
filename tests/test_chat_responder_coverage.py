"""Offline CHAT-1 acceptance, with synthetic HR messages and fake transports."""
import asyncio
import copy
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import hh_chat_responder as cr
from hh.chat import ChatList
from runtime_context import ChatResponderLimits, RuntimePaths
from tests.chat_ownership_fakes import owned_chats

native_get_messages = cr.get_messages


@pytest.fixture
def chat_run(tmp_path, monkeypatch):
    import hh_client
    import hiring_research
    import notifier
    paths = RuntimePaths(str(tmp_path), str(tmp_path / "state"), str(tmp_path / "resume.md"))
    owned_chats(monkeypatch, tmp_path, ["42", "ai", "hr", "invite", "form"])
    messages = {"42": {"messages": [dict(id="1", text="Когда готовы приступить?", is_me=False)],
                        "vacancy": {"title": "Synthetic QA", "company": "Synthetic Company"}}}
    rows = ChatList([dict(chat_id="42", preview="Synthetic preview")],
                    list_complete=True, stop_reason="stable_bottom")
    opened, notifications = [], []
    page = SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock())
    async def read(page, chat_id):
        opened.append(chat_id)
        value = messages[chat_id]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)
    async def notify(text, **kwargs):
        notifications.append((text, kwargs.get("reply_markup")))
        return True
    monkeypatch.setattr(hh_client, "_load_resume_text", lambda: "Synthetic resume")
    monkeypatch.setattr(cr, "_get_llm_client", lambda: None)
    monkeypatch.setattr(cr, "_active_profile_name", lambda: "synthetic")
    monkeypatch.setattr(cr, "list_chats", AsyncMock(side_effect=lambda page: rows))
    monkeypatch.setattr(cr, "get_messages", read)
    monkeypatch.setattr(hiring_research, "record_screening", lambda *args: None)
    monkeypatch.setattr(notifier, "send_message_with_markup", notify)
    generate = AsyncMock(side_effect=AssertionError("No model call during HR/invite scan"))
    send = AsyncMock(side_effect=AssertionError("No HH send during HR/invite scan"))
    monkeypatch.setattr(cr, "generate_answer", generate)
    monkeypatch.setattr(cr, "send_message", send)
    monkeypatch.setattr(cr.asyncio, "sleep", AsyncMock())
    client = SimpleNamespace(_page=page)
    async def run(limit=25):
        return await cr.process_all(client, dry_run=False, runtime_paths=paths,
            limits=ChatResponderLimits(5, 0, limit))
    def events():
        return [json.loads(line) for line in (tmp_path / "chat_journal.jsonl").read_text().splitlines()]
    return SimpleNamespace(paths=paths, messages=messages, rows=rows, opened=opened,
        notifications=notifications, generate=generate, send=send, run=run, events=events,
        client=client, page=page)


def test_chat_hr_card_deduplicates_per_incoming_without_model_or_send(chat_run):
    first = asyncio.run(chat_run.run())
    assert first["decisions"] == {"hr_question": 1} and first["hr_notified"] == 1
    text, markup = chat_run.notifications[0]
    assert "Synthetic Company" in text and "Когда готовы приступить?" in text
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    assert {button.get("callback_data") for button in buttons} == {None, "chat_ai_any:synthetic:42:1"}
    assert asyncio.run(chat_run.run())["decisions"] == {"already_handled": 1}
    chat_run.messages["42"]["messages"][0]["text"] = "Edited synthetic question?"
    assert asyncio.run(chat_run.run())["decisions"] == {"already_handled": 1}
    chat_run.messages["42"]["messages"].append(dict(id="2", text="Пришлите портфолио", is_me=False))
    assert asyncio.run(chat_run.run())["hr_notified"] == 1
    assert len(chat_run.notifications) == 2
    chat_run.generate.assert_not_awaited()
    chat_run.send.assert_not_awaited()


def test_chat_google_form_remains_on_its_existing_path_without_followup_hr_card(chat_run, monkeypatch):
    chat_run.messages["42"]["messages"][0]["text"] = "Заполните https://forms.gle/synthetic"
    form = AsyncMock(return_value=dict(ok=True, message_id="1"))
    monkeypatch.setattr(cr, "_prepare_google_form_preview_from_message", form)
    assert asyncio.run(chat_run.run())["decisions"] == {"google_form": 1}
    assert asyncio.run(chat_run.run())["decisions"] == {"already_handled": 1}
    assert chat_run.notifications == []
    form.assert_awaited_once()
    chat_run.generate.assert_not_awaited()


@pytest.mark.parametrize("url", ["t.me/synthetic_bot", "telegram.me/synthetic_bot",
    "tg://resolve?domain=synthetic_bot", "https://example.org/test/42"])
def test_chat_invite_card_has_url_and_no_ai_action(chat_run, url):
    chat_run.messages["42"]["messages"][0]["text"] = f"Пройдите тест: {url}"
    result = asyncio.run(chat_run.run())
    assert result["decisions"] == {"external_invite": 1}
    text, markup = chat_run.notifications[0]
    assert "Приглашение пройти:" in text
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    assert all("callback_data" not in button for button in buttons)
    assert any(button["url"].endswith(url) or button["url"] == url for button in buttons)
    assert asyncio.run(chat_run.run())["decisions"] == {"already_handled": 1}
    chat_run.generate.assert_not_awaited()
    chat_run.send.assert_not_awaited()
    assert chat_run.opened == ["42", "42"]  # No extra opens for statistics or invitations.


def test_chat_every_discovered_chat_has_one_logged_decision_and_private_snapshot(chat_run, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="chat_responder")
    content = "Synthetic private question? " * 35
    cases = [
        ("ai", [dict(id="a", text="AI question", is_ai=True)], "ai_hh_autoreply"),
        ("hr", [dict(id="h", text=content, author="HR")], "hr_question"),
        ("invite", [dict(id="i", text="t.me/synthetic_bot")], "external_invite"),
        ("form", [dict(id="f", text="https://forms.gle/synthetic")], "google_form"),
        ("ours", [dict(id="o", text="Our synthetic message", is_me=True)], "ours_last"),
        ("answered", [dict(id="q", text="Question?"), dict(id="r", is_me=True)], "ours_last"),
        ("system", [dict(id="s", text="Работодатель просмотрел ваше резюме")], "system_notice"),
        ("unknown", [], "unknown"),
        ("failed", [], "read_failed"),
        ("outside", [], "out_of_batch"),
    ]
    chat_run.rows[:] = [dict(chat_id=cid, preview="Synthetic", raw_text=content) for cid, _, _ in cases]
    chat_run.rows.append(dict(chat_id="filtered", preview="Отклик на вакансию", raw_text="Отклик на вакансию"))
    for cid, msgs, _ in cases:
        chat_run.messages[cid] = {"messages": msgs, "vacancy": {}}
    chat_run.messages["failed"] = TimeoutError("Private error body must not reach logs")
    execute = AsyncMock(return_value=dict(ok=True, answer="Synthetic answer", sent=True))
    form = AsyncMock(return_value=dict(ok=True, message_id="f"))
    monkeypatch.setattr(cr, "_execute_reply", execute)
    monkeypatch.setattr(cr, "_prepare_google_form_preview_from_message", form)
    result = asyncio.run(chat_run.run(limit=9))
    events = chat_run.events()
    decisions = [event for event in events if event["event"] == "chat_decision"]
    assert len(decisions) == len(chat_run.rows) == sum(result["decisions"].values())
    assert {event["chat_id"]: event["decision"] for event in decisions} == {
        **{cid: expected for cid, _, expected in cases}, "filtered": "filtered"}
    refs = {event["snapshot_id"] for event in events if event["event"] != "chat_decision"}
    assert all(event["snapshot_ref"] in refs for event in decisions)
    assert events[0]["rows"][0]["raw_text"] == content
    assert events[0]["list_complete"] is True and events[0]["stop_reason"] == "stable_bottom"
    thread = next(event for event in events if event["event"] == "chat_thread_snapshot" and event["chat_id"] == "hr")
    assert thread["messages"][0]["text"] == content and thread["history_complete"] is False
    assert content not in caplog.text and "Private error body" not in caplog.text
    assert len([record for record in caplog.records if record.message.startswith("chat decision")]) == 11
    assert len(chat_run.opened) == 9
    execute.assert_awaited_once()
    form.assert_awaited_once()


def test_chat_rotation_through_process_all_preserves_open_limit(chat_run):
    chat_run.rows[:] = [dict(chat_id=str(i), preview="Synthetic") for i in range(60)]
    chat_run.rows[55]["unread"] = True
    chat_run.messages.update({str(i): {"messages": [], "vacancy": {}} for i in range(60)})
    for _ in range(3):
        assert asyncio.run(chat_run.run())["chats_scanned"] == 25
    assert chat_run.opened[0] == "55" and len(chat_run.opened) == 75
    assert set(chat_run.opened) == {str(i) for i in range(60)}


@pytest.mark.parametrize("failure", ["false", "raise", "cancel"])
def test_chat_notice_uncertain_outcome_is_never_automatically_repeated(chat_run, monkeypatch, failure):
    import notifier
    async def notify(*args, **kwargs):
        if failure == "raise":
            raise RuntimeError("Private notification payload")
        if failure == "cancel":
            raise asyncio.CancelledError
        return False
    mocked = AsyncMock(side_effect=notify)
    monkeypatch.setattr(notifier, "send_message_with_markup", mocked)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(chat_run.run())
    else:
        asyncio.run(chat_run.run())
    assert asyncio.run(chat_run.run())["decisions"] == {"already_handled": 1}
    assert mocked.await_count == 1


def test_chat_hr_button_preview_alternative_revision_and_approved_send_use_existing_path(chat_run, monkeypatch):
    assert asyncio.run(chat_run.run())["hr_notified"] == 1
    chat_run.generate.assert_not_awaited()
    generated = AsyncMock(return_value="Synthetic grounded answer")
    monkeypatch.setattr(cr, "generate_answer", generated)
    monkeypatch.setattr(cr, "fill_and_preview", AsyncMock(return_value=dict(filled=True)))
    monkeypatch.setattr(cr, "_extract_messages", AsyncMock(side_effect=lambda page: copy.deepcopy(chat_run.messages["42"])))
    async def send(*args, **kwargs):
        await kwargs["before_send"]()
        return True
    sent = AsyncMock(side_effect=send)
    monkeypatch.setattr(cr, "send_message", sent)
    async def manual(**kwargs):
        return await cr.process_one(chat_run.client, "42", allow_any=True,
            runtime_paths=chat_run.paths, **kwargs)
    preview = asyncio.run(manual(message_id="1", dry_run=True))
    assert preview["ok"] and generated.await_args.kwargs["question_kind"] == "ручное HR-сообщение"
    alternative = asyncio.run(manual(message_id="1", dry_run=True, alternative=True))
    assert alternative["ok"] and generated.await_args.kwargs["alternative"] is True
    assert alternative["draft_revision"] != preview["draft_revision"]
    stale = asyncio.run(manual(message_id="1~" + preview["draft_revision"], dry_run=False))
    assert stale["stale"] and sent.await_count == 0
    approved = asyncio.run(manual(message_id="1~" + alternative["draft_revision"], dry_run=False))
    assert approved["sent"] and sent.await_count == 1 and generated.await_count == 2


def test_chat_stopped_run_still_accounts_for_every_discovered_chat(chat_run, monkeypatch):
    chat_run.rows.append(dict(chat_id="unopened", preview="Synthetic"))
    monkeypatch.setattr(cr, "get_messages", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(chat_run.run())
    decisions = [event for event in chat_run.events() if event["event"] == "chat_decision"]
    assert len(decisions) == 2
    assert decisions[0]["reason"] == "processing_interrupted:CancelledError"
    assert decisions[1]["reason"] == "run_stopped_before_open"
    assert decisions[1]["decision"] == "out_of_batch"


def test_chat_diagnostic_flag_captures_hr_and_ours_once_without_extra_opens(chat_run, monkeypatch):
    from pathlib import Path
    monkeypatch.setenv("HH_CHAT_DIAGNOSTIC_SNAPSHOTS", "1")
    chat_run.page.evaluate = AsyncMock(return_value=[dict(
        kind="message", data_qa="chatik-chat-message-1",
        html="<div>synthetic DOM</div>", truncated=False)])
    chat_run.page.screenshot = AsyncMock(return_value=b"synthetic PNG")
    asyncio.run(chat_run.run())
    asyncio.run(chat_run.run())
    chat_run.messages["42"]["messages"].append(dict(id="2", text="Synthetic answer", is_me=True))
    asyncio.run(chat_run.run())
    asyncio.run(chat_run.run())
    root = Path(chat_run.paths.home_dir) / "chat_diagnostics"
    assert len(list(root.glob("*.png"))) == 2
    assert chat_run.page.screenshot.await_count == 2 and len(chat_run.opened) == 4
    refs = {event["snapshot_id"] for event in chat_run.events() if event["event"] == "chat_thread_snapshot"}
    for path in root.glob("*.json"):
        if path.name != "state.json":
            assert json.loads(path.read_text())["snapshot_ref"] in refs


def test_chat_review_card_escapes_task_name_and_bounds_telegram_html(chat_run):
    message = chat_run.messages["42"]["messages"][0]
    message["text"] = 'Пройдите тест <Python> & SQL: https://example.org/test/42 ' + '"' * 2000
    chat_run.messages["42"]["vacancy"] = dict(title='"' * 200, company='"' * 200)
    asyncio.run(chat_run.run())
    text, markup = chat_run.notifications[0]
    assert "тест &lt;Python&gt; &amp; SQL" in text and "<Python>" not in text
    assert len(text) <= 4096
    assert all("callback_data" not in button for row in markup["inline_keyboard"] for button in row)


def test_chat_existing_freshness_read_is_journalled_without_extra_navigation(chat_run, monkeypatch):
    from chat_observation import active_chat_journal
    calls = []
    async def read(page, chat_id, **kwargs):
        calls.append(chat_id)
        return dict(messages=[dict(id="1", text="Old question?" if len(calls) == 1 else "New question?",
                                   is_ai=True, is_me=False)], vacancy={})
    monkeypatch.setattr(cr, "get_messages", native_get_messages)
    monkeypatch.setattr(cr, "_chat_get_messages", read)
    monkeypatch.setattr(cr, "generate_answer", AsyncMock(return_value="Synthetic answer"))
    result = asyncio.run(chat_run.run())
    assert result["answers_sent"] == 0 and calls == ["42", "42"]
    snapshots = [event for event in chat_run.events() if event["event"] == "chat_thread_snapshot"]
    assert [event["messages"][0]["text"] for event in snapshots] == ["Old question?", "New question?"]
    decision = [event for event in chat_run.events() if event["event"] == "chat_decision"][0]
    assert decision["reason"] == "stale_question" and decision["snapshot_ref"] == snapshots[-1]["snapshot_id"]
    assert active_chat_journal.get() is None
    chat_run.send.assert_not_awaited()


def test_hr_message_in_rejected_chat_goes_to_one_digest_without_reply_card(chat_run, monkeypatch, tmp_path):
    """B1 (18.10): 7 of 11 «Вопрос HR» cards were chats the employer had already rejected."""
    import config
    import notifier
    home = tmp_path
    (home / "negotiation_snapshots.jsonl").write_text(json.dumps(
        {"chatId": 42, "resumeId": "111", "lastState": "DISCARD", "company_name": "Snapshot Company", "vacancy_name": "Snapshot QA"}) + "\n")
    chat_run.messages["42"]["vacancy"] = {"title": "Перейти", "company": "—"}
    chat_run.messages["42"]["messages"][0]["text"] = "К сожалению, мы не готовы пригласить вас. Есть вопросы?"
    digests = []

    async def send_message(text, **kwargs):
        digests.append(text)
        return True

    monkeypatch.setattr(notifier, "send_message", send_message)
    first = asyncio.run(chat_run.run())
    assert chat_run.notifications == []  # no «Ответить с ИИ» card
    assert len(digests) == 1 and "Отказы в чатах: 1" in digests[0] and "Snapshot Company — Snapshot QA" in digests[0]
    assert first["rejections"] == [{"title": "Snapshot QA", "company": "Snapshot Company"}]
    assert first["hr_notified"] == 0  # a digest line is not a reply card
    assert asyncio.run(chat_run.run())["decisions"] == {"already_handled": 1}
    assert len(digests) == 1  # each rejection once


def test_chat_of_another_profile_gets_no_card(chat_run, monkeypatch, tmp_path):
    """B13 (dev 19.10): electrician profile answered QA chats with electrician facts."""
    import config
    home = tmp_path
    (home / "directions" / "synthetic.json").unlink()
    (home / "negotiation_snapshots.jsonl").write_text(json.dumps({"chatId": 42, "resumeId": 111}) + "\n")
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(home))
    summary = asyncio.run(chat_run.run())
    assert chat_run.notifications == [] and summary["decisions"] == {"other_profile": 1}
