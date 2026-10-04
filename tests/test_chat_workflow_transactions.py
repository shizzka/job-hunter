"""Browser/model/notifier awaits are fakes; runtime data lives in tmp_path."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

import hh_chat_responder as cr
from runtime_context import ChatResponderLimits, RuntimePaths


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    import hh_client
    import hiring_research
    import notifier
    paths = RuntimePaths(home_dir=str(tmp_path), resume_file=str(tmp_path / "resume.md"),
                         hh_state_dir=str(tmp_path / "state"))
    repo = cr._state_repository(paths)
    message = {"id": "m1", "text": "Synthetic question", "author": "Robot",
               "is_ai": True, "is_me": False}
    data = {"messages": [message], "vacancy": {"title": "Synthetic QA", "company": "Example"}}
    calls = []
    async def noop(*args, **kwargs):
        pass
    page = SimpleNamespace(goto=noop, wait_for_timeout=noop)
    client = SimpleNamespace(_page=page)
    async def messages(*args, **kwargs):
        return copy.deepcopy(data)
    async def answer(*args, **kwargs):
        calls.append("model")
        return "Synthetic answer"
    async def preview(*args, **kwargs):
        calls.append("preview")
        assert kwargs["runtime_paths"] == paths
        return {"filled": True, "screenshot_path": ""}
    async def send(*args, **kwargs):
        calls.append("send")
        if "before_send" in kwargs:
            await kwargs["before_send"]()
        calls.append("click")
        return True
    async def notify(*args, **kwargs):
        calls.append("notify")
        return True
    async def chats(*args):
        return [{"chat_id": "chat", "preview": "Synthetic question"}]
    monkeypatch.setattr(hh_client, "_load_resume_text", lambda: "Synthetic resume")
    monkeypatch.setattr(hiring_research, "record_screening", lambda *args: None)
    monkeypatch.setattr(cr, "_active_profile_name", lambda: "synthetic")
    monkeypatch.setattr(cr, "get_messages", messages)
    monkeypatch.setattr(cr, "_extract_messages", messages)
    monkeypatch.setattr(cr, "list_chats", chats)
    monkeypatch.setattr(cr, "generate_answer", answer)
    monkeypatch.setattr(cr, "fill_and_preview", preview)
    monkeypatch.setattr(cr, "send_message", send)
    monkeypatch.setattr(notifier, "send_message_with_markup", notify)
    monkeypatch.setattr(cr.asyncio, "sleep", noop)
    async def run(*, dry_run=False, notify=False):
        return await cr.process_one(client, "chat", message_id="m1", dry_run=dry_run,
                                    notify=notify, runtime_paths=paths)
    async def run_all(*, dry_run=True):
        return await cr.process_all(client, dry_run=dry_run, runtime_paths=paths,
                                   limits=ChatResponderLimits(max_replies_per_chat=5, reply_cooldown_s=0))
    return SimpleNamespace(repo=repo, paths=paths, calls=calls, data=data, run=run,
                           run_all=run_all, client=client)


def attempt_status(workflow):
    return next(iter(workflow.repo.load()["chat"]["attempts"].values()))["status"]


def test_process_one_preserves_concurrent_other_chat(workflow, monkeypatch):
    async def answer(*args, **kwargs):
        # Compatibility writer also exists on the pre-fix baseline. It reads
        # fresh state and adds another chat while process_one holds its snapshot.
        state = workflow.repo.load()
        state["other"] = {"fresh": True}
        workflow.repo.save(state)
        return "Synthetic answer"
    monkeypatch.setattr(cr, "generate_answer", answer)
    result = asyncio.run(workflow.run())
    assert result["sent"]
    state = workflow.repo.load()
    assert state["other"] == {"fresh": True}
    assert state["chat"]["replies_count"] == 1
    assert attempt_status(workflow) == "completed"
    assert asyncio.run(workflow.run())["already_replied"]
    assert workflow.calls.count("click") == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_duplicate_manual_during_model_wait_is_blocked(workflow, monkeypatch, dry_run):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def answer(*args, **kwargs):
            entered.set()
            await release.wait()
            return "Synthetic answer"
        monkeypatch.setattr(cr, "generate_answer", answer)
        first = asyncio.create_task(workflow.run(dry_run=dry_run))
        await entered.wait()
        second = await workflow.run(dry_run=dry_run)
        assert second["blocked"]
        release.set()
        assert (await first)["ok"]
    asyncio.run(scenario())
    assert workflow.calls.count("click" if not dry_run else "preview") == 1


def test_scan_cannot_duplicate_manual_claim(workflow, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def answer(*args, **kwargs):
            entered.set()
            await release.wait()
            return "Synthetic answer"
        monkeypatch.setattr(cr, "generate_answer", answer)
        first = asyncio.create_task(workflow.run())
        await entered.wait()
        summary = await workflow.run_all(dry_run=False)
        assert summary["answers_sent"] == 0
        assert summary["skipped"] == 1
        release.set()
        await first
    asyncio.run(scenario())
    assert workflow.calls.count("click") == 1


@pytest.mark.parametrize("change", ["id", "text", "is_me", "links"])
def test_changed_question_after_model_is_not_sent(workflow, monkeypatch, change):
    async def answer(*args, **kwargs):
        workflow.data["messages"][-1][change] = {"id": "new", "text": "Changed question",
                                                "is_me": True, "links": ["changed"]}[change]
        return "Synthetic answer"
    monkeypatch.setattr(cr, "generate_answer", answer)
    assert asyncio.run(workflow.run())["stale"]
    assert "send" not in workflow.calls
    assert attempt_status(workflow) == "failed"


def test_changed_question_during_browser_fill_blocks_click(workflow, monkeypatch):
    async def send(*args, **kwargs):
        workflow.data["messages"][-1]["text"] = "New question during input fill"
        await kwargs["before_send"]()
        pytest.fail("Click must not happen")
    monkeypatch.setattr(cr, "send_message", send)
    with pytest.raises(RuntimeError, match="changed before send"):
        asyncio.run(workflow.run())
    assert attempt_status(workflow) == "failed"


def test_stale_approval_for_nonlatest_message_blocks_model(workflow):
    workflow.data["messages"].append({"id": "m2", "text": "New", "is_me": False})
    result = asyncio.run(workflow.run())
    assert not result["ok"]
    assert "model" not in workflow.calls


@pytest.mark.parametrize("stage", ["model", "fill", "click", "verification"])
def test_cancel_before_and_after_send_boundary(workflow, monkeypatch, stage):
    async def answer(*args, **kwargs):
        raise asyncio.CancelledError
    async def send(*args, **kwargs):
        if stage in {"click", "verification"}:
            await kwargs["before_send"]()
        if stage == "verification":
            workflow.calls.append("external-sent")
        raise asyncio.CancelledError
    if stage == "model":
        monkeypatch.setattr(cr, "generate_answer", answer)
    else:
        monkeypatch.setattr(cr, "send_message", send)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(workflow.run())
    assert attempt_status(workflow) == ("failed" if stage in {"model", "fill"} else "uncertain")
    if stage in {"click", "verification"}:
        workflow.data["messages"][-1]["id"] = "m2"
        assert workflow.repo.claim("chat", "reply", "m2") is None


@pytest.mark.parametrize("possible_click", [False, True])
def test_false_send_result_has_correct_retry_boundary(workflow, monkeypatch, possible_click):
    async def send(*args, **kwargs):
        if possible_click:
            await kwargs["before_send"]()
        return False
    monkeypatch.setattr(cr, "send_message", send)
    assert not asyncio.run(workflow.run())["ok"]
    assert attempt_status(workflow) == ("uncertain" if possible_click else "failed")


def test_claim_write_failure_prevents_model_and_browser(workflow, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic claim write failure")
    monkeypatch.setattr("state_store.json_store.os.replace", fail)
    with pytest.raises(OSError):
        asyncio.run(workflow.run())
    assert workflow.calls == []


def test_phase_write_failure_prevents_click(workflow, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic phase failure")
    monkeypatch.setattr(workflow.repo.__class__, "mark_acting", fail)
    with pytest.raises(OSError):
        asyncio.run(workflow.run())
    assert "click" not in workflow.calls
    assert attempt_status(workflow) == "failed"


def test_completion_failure_leaves_nonretryable_send(workflow, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic completion failure")
    monkeypatch.setattr(workflow.repo.__class__, "finish", fail)
    with pytest.raises(OSError):
        asyncio.run(workflow.run())
    assert "click" in workflow.calls
    assert attempt_status(workflow) == "uncertain"
    assert workflow.repo.claim("chat", "reply", "m1") is None


def test_profile_switch_does_not_redirect_result(workflow, tmp_path, monkeypatch):
    other = tmp_path / "other" / "resume.md"
    async def answer(*args, **kwargs):
        monkeypatch.setattr(cr.config, "RESUME_FILE", str(other))
        return "Synthetic answer"
    monkeypatch.setattr(cr, "generate_answer", answer)
    assert asyncio.run(workflow.run())["sent"]
    assert workflow.repo.load()["chat"]["replies_count"] == 1
    assert not (other.parent / "chat_responder_state.json").exists()


def test_scan_preview_notification_claim_prevents_reentry(workflow, monkeypatch):
    import notifier
    async def notify(*args, **kwargs):
        workflow.repo.update_chat("other", lambda chat: chat.update(fresh=True))
        assert workflow.repo.claim("chat", "preview", "m1") is None
        return True
    monkeypatch.setattr(notifier, "send_message_with_markup", notify)
    first = asyncio.run(workflow.run_all())
    second = asyncio.run(workflow.run_all())
    assert first["answers_drafted"] == 1
    assert second["answers_drafted"] == 0
    assert workflow.repo.load()["other"] == {"fresh": True}
    assert workflow.repo.load()["chat"]["last_previewed_msg_id"] == "m1"


def test_preview_notification_cancellation_is_not_replayed(workflow, monkeypatch):
    import notifier
    async def notify(*args, **kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(notifier, "send_message_with_markup", notify)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(workflow.run_all())
    assert attempt_status(workflow) == "uncertain"
    assert asyncio.run(workflow.run_all())["answers_drafted"] == 0


def test_send_notification_failure_keeps_durable_counter(workflow, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic notification failure")
    monkeypatch.setattr(cr, "_notify_one_chat_result", fail)
    assert asyncio.run(workflow.run(notify=True))["sent"]
    assert workflow.repo.load()["chat"]["replies_count"] == 1
    assert attempt_status(workflow) == "completed"


def test_unexpected_ui_stops_entire_scan(workflow, monkeypatch):
    async def fail(*args, **kwargs):
        raise cr.HHUnexpectedUI("synthetic", "a" * 64)
    monkeypatch.setattr(cr, "get_messages", fail)
    with pytest.raises(cr.HHUnexpectedUI):
        asyncio.run(workflow.run_all())
    assert workflow.calls == []


def test_lost_owner_before_click_prevents_send(workflow, monkeypatch):
    async def extract(*args, **kwargs):
        state = workflow.repo.load()
        attempt = next(iter(state["chat"]["attempts"].values()))
        attempt["owner"] = "a" * 32
        workflow.repo.save(state)
        return copy.deepcopy(workflow.data)
    monkeypatch.setattr(cr, "_extract_messages", extract)
    with pytest.raises(RuntimeError, match="ownership lost"):
        asyncio.run(workflow.run())
    assert "click" not in workflow.calls
    assert attempt_status(workflow) == "preparing"


def test_suspicious_notification_preserves_concurrent_state(workflow, monkeypatch):
    workflow.data["messages"][-1].update(is_ai=False, is_ai_suspect=True)
    async def notify(*args, **kwargs):
        assert kwargs["profile_name"] == "synthetic"
        workflow.repo.update_chat("other", lambda chat: chat.update(fresh=True))
        assert workflow.repo.claim("chat", "notice", "m1") is None
        return True
    monkeypatch.setattr(cr, "notify_suspicious_screening_message", notify)
    assert asyncio.run(workflow.run_all())["suspicious_notified"] == 1
    assert asyncio.run(workflow.run_all())["suspicious_notified"] == 0
    assert workflow.repo.load()["other"] == {"fresh": True}


def test_form_preview_preserves_concurrent_state_and_deduplicates(workflow, monkeypatch):
    workflow.data["messages"][-1]["text"] = "https://forms.gle/synthetic"
    calls = []
    async def prepare(*args, **kwargs):
        assert kwargs["runtime_paths"] == workflow.paths
        assert kwargs["profile_name"] == "synthetic"
        calls.append(True)
        workflow.repo.update_chat("other", lambda chat: chat.update(fresh=True))
        return {"ok": True, "token": "synthetic", "message_id": "m1"}
    monkeypatch.setattr(cr, "_prepare_google_form_preview_from_message", prepare)
    assert asyncio.run(workflow.run_all())["google_forms_prepared"] == 1
    assert asyncio.run(workflow.run_all())["google_forms_prepared"] == 0
    assert calls == [True]
    assert workflow.repo.load()["other"] == {"fresh": True}


@pytest.mark.parametrize("kind", ["notice", "form"])
def test_cancelled_notification_or_form_is_not_replayed(workflow, monkeypatch, kind):
    if kind == "form":
        workflow.data["messages"][-1]["text"] = "https://forms.gle/synthetic"
        target = "_prepare_google_form_preview_from_message"
    else:
        workflow.data["messages"][-1].update(is_ai=False, is_ai_suspect=True)
        target = "notify_suspicious_screening_message"
    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(cr, target, cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(workflow.run_all())
    assert attempt_status(workflow) == "uncertain"
    assert asyncio.run(workflow.run_all())["skipped"] == 1


@pytest.mark.parametrize("quick_reply", [False, True])
def test_low_level_claim_happens_after_ui_wait_before_click(monkeypatch, quick_reply):
    import hh.chat as low
    calls = []
    async def fill(*args):
        calls.append("fill")
        return {"filled": True, "quick_reply": "Да" if quick_reply else ""}
    async def ui(*args):
        calls.append("ui")
    async def wait(*args):
        calls.append("wait")
    async def click():
        assert calls[-1] == "claim"
        calls.append("click")
    async def evaluate(script, expected=None):
        if 'codex:chat-send-arm' in script:
            calls.append("arm")
            return expected['text'] == ("Да" if quick_reply else "Synthetic answer")
        if 'codex:chat-send-readback' in script:
            return "click" in calls
        raise AssertionError("Unexpected browser guard script")
    button = SimpleNamespace(click=click, evaluate=evaluate)
    async def find(*args):
        calls.append("button")
        return button
    async def before_send():
        await wait()
        calls.append("claim")
    async def extract(*args):
        return {"messages": [{"is_me": True, "text": "Да" if quick_reply else "Synthetic answer"}]}
    monkeypatch.setattr(low, "ensure_page_ui", ui)
    page = SimpleNamespace(query_selector=find, wait_for_timeout=wait)
    assert asyncio.run(low.send_message(page, "chat", "Да" if quick_reply else "Synthetic answer", fill_preview=fill,
        find_quick_reply=find, before_send=before_send, extract_current_messages=extract))
    assert calls.index("fill") < calls.index("button") < calls.index("arm") < calls.index("claim") < calls.index("click")
