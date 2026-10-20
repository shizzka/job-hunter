"""B22: shared HH visibility never authorizes another resume's candidate facts."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import chat_context as cc
import google_form_filler as forms
import hh_chat_responder as cr
from tests.test_chat_workflow_transactions import workflow, salary_workflow


def snapshot_path(w):
    return Path(w.paths.home_dir) / "negotiation_snapshots.jsonl"


def set_resume(w, resume):
    snapshot_path(w).write_text(json.dumps({"chatId": "chat", "resumeId": resume}) + "\n")


def deny_owner(w, monkeypatch, case):
    if case == "unknown":
        snapshot_path(w).unlink()
    elif case == "lookup_error":
        monkeypatch.setattr(cc, "resolve", Mock(side_effect=OSError("Synthetic lookup failure")))
    else:
        set_resume(w, "333" if case == "other" else "222")


@pytest.mark.parametrize("case", ["unknown", "lookup_error", "other", "direction"])
@pytest.mark.parametrize("entry", ["scanner", "one", "scanner_form", "direct_form", "raw_form"])
def test_unbound_thread_never_generates_sends_or_previews(workflow, monkeypatch, case, entry):
    w = workflow
    from hh import negotiation_snapshot
    refresh = Mock(return_value={"ok": True})
    monkeypatch.setattr(negotiation_snapshot, "collect", refresh)
    deny_owner(w, monkeypatch, case)
    model = AsyncMock(side_effect=AssertionError("Unbound facts reached generation"))
    send = AsyncMock(side_effect=AssertionError("Unbound reply reached send"))
    preview = AsyncMock(side_effect=AssertionError("Unbound facts reached form preview"))
    monkeypatch.setattr(cr, "generate_answer", model)
    monkeypatch.setattr(cr, "send_message", send)
    monkeypatch.setattr(cr, "_prepare_google_form_preview_from_message", preview)
    if entry != "raw_form":
        monkeypatch.setattr(forms, "preview_form", preview)
    monkeypatch.setattr(forms, "generate_form_answers", preview)
    if "form" in entry:
        w.data["messages"][-1]["text"] = "Заполните https://forms.gle/synthetic"
    if entry.startswith("scanner"):
        result = asyncio.run(w.run_all(dry_run=False))
        assert result["answers_drafted"] == result["answers_sent"] == result["google_forms_prepared"] == 0
    elif entry == "raw_form":
        result = asyncio.run(forms.preview_form(w.client._page, "https://forms.gle/synthetic",
                            chat_id="chat", profile_name="synthetic", runtime_paths=w.paths))
        assert not result["ok"]
    elif entry == "direct_form":
        result = asyncio.run(forms.preview_from_hh_chat(w.client, "chat", message_id="m1",
                            profile_name="synthetic", runtime_paths=w.paths, notify=True))
        assert not result["ok"]
    else:
        result = asyncio.run(w.run(notify=True))
        assert not result["ok"]
    model.assert_not_awaited()
    send.assert_not_awaited()
    preview.assert_not_awaited()
    refresh.assert_not_called()
    if case in {"unknown", "lookup_error"} and entry != "raw_form":
        assert "notify" in w.calls


@pytest.mark.parametrize("entry", ["scanner", "one", "direct_form"])
def test_reply_never_collects_snapshots_per_message(workflow, monkeypatch, entry):
    from hh import negotiation_snapshot
    snapshot_path(workflow).unlink()
    refresh = Mock(side_effect=OSError("Synthetic refresh failure"))
    monkeypatch.setattr(negotiation_snapshot, "collect", refresh)
    if entry == "scanner":
        assert asyncio.run(workflow.run_all())["answers_drafted"] == 0
    elif entry == "one":
        assert asyncio.run(workflow.run(dry_run=True))["reason"] == "unknown_owner"
    else:
        assert not asyncio.run(forms.preview_from_hh_chat(workflow.client, "chat",
                              runtime_paths=workflow.paths))["ok"]
    assert "model" not in workflow.calls and "send" not in workflow.calls
    refresh.assert_not_called()


@pytest.mark.parametrize("entry", ["scanner", "one", "direct_form"])
def test_periodic_refresh_can_establish_positive_binding(workflow, monkeypatch, entry):
    from hh import negotiation_snapshot
    w = workflow
    snapshot_path(w).unlink()
    def collect(**kwargs):
        set_resume(w, "111")
        return {"complete": True, "stop_reason": None}
    refresh = Mock(side_effect=collect)
    monkeypatch.setattr(negotiation_snapshot, "collect", refresh)
    negotiation_snapshot.refresh_for_chats(w.paths.home_dir, cr.config.HH_COOKIES_FILE)
    if entry == "scanner":
        assert asyncio.run(w.run_all(dry_run=False))["answers_sent"] == 1
    elif entry == "one":
        assert asyncio.run(w.run())["sent"]
    else:
        from types import SimpleNamespace
        w.data["messages"][-1]["text"] = "https://forms.gle/synthetic"
        page = SimpleNamespace(close=AsyncMock())
        w.client._page.context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        async def preview(*args, **kwargs):
            from answer_grounding import current_candidate
            kwargs["ownership_binding"].check(current_candidate())
            return {"ok": True}
        model = AsyncMock(side_effect=preview)
        monkeypatch.setattr(forms, "preview_form", model)
        assert asyncio.run(forms.preview_from_hh_chat(w.client, "chat", profile_name="synthetic",
                           runtime_paths=w.paths))["ok"]
        model.assert_awaited_once()
    assert refresh.call_count == 2
    assert [call.kwargs["archived"] for call in refresh.call_args_list] == [False, True]
    assert all(call.kwargs["max_pages"] == 3 for call in refresh.call_args_list)


@pytest.mark.parametrize("when", ["model", "before_send"])
@pytest.mark.parametrize("resume", ["", "222", "333", "112"])
def test_final_send_rechecks_captured_resume_and_direction(workflow, monkeypatch, when, resume):
    w = workflow
    # A second resume in the same direction must not replace the captured binding.
    direction = Path(w.paths.home_dir) / "directions" / "qa.json"
    value = json.loads(direction.read_text())
    value["variants"].append({"hh_resume_numeric_id": "112"})
    direction.write_text(json.dumps(value))
    def change():
        set_resume(w, resume)
    async def answer(*args, **kwargs):
        change()
        return "Synthetic answer"
    async def send(*args, **kwargs):
        change()
        await kwargs["before_send"]()
        pytest.fail("Ownership change must prevent click")
    monkeypatch.setattr(cr, "generate_answer" if when == "model" else "send_message",
                        answer if when == "model" else send)
    with pytest.raises(RuntimeError, match="ownership changed"):
        asyncio.run(w.run())
    assert "click" not in w.calls


def test_native_valid_owner_and_stale_message_controls(salary_workflow, monkeypatch):
    w = salary_workflow
    from tests.test_chat_workflow_transactions import SALARY_SOURCE
    model = w.model_for(quote=SALARY_SOURCE)
    assert asyncio.run(w.run())["sent"]
    assert len(model.calls) == 2 and w.calls.count("click") == 1
    w.data["messages"].append({"id": "m2", "text": "New synthetic question", "is_me": False})
    assert asyncio.run(w.run())["reason"] == "stale_question"
    assert len(model.calls) == 2 and w.calls.count("click") == 1


def test_ownership_review_is_plain_and_deduplicated(workflow, monkeypatch):
    from hh import negotiation_snapshot
    import notifier
    snapshot_path(workflow).unlink()
    monkeypatch.setattr(negotiation_snapshot, "collect", Mock())
    notice = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_message_with_markup", notice)
    for _ in range(2):
        assert asyncio.run(workflow.run_all())["answers_drafted"] == 0
    notice.assert_awaited_once()
    assert cc.UNKNOWN_TEXT in notice.call_args.args[0]
    assert "model" not in workflow.calls


def test_cookie_mismatch_still_blocks_before_ownership_refresh(workflow, monkeypatch):
    from hh import negotiation_snapshot
    from types import SimpleNamespace
    workflow.client._cookie_paths = SimpleNamespace(cookies_file="/synthetic/wrong-cookies")
    refresh = Mock()
    monkeypatch.setattr(negotiation_snapshot, "collect", refresh)
    with pytest.raises(ValueError, match="profile/account mismatch"):
        asyncio.run(workflow.run())
    refresh.assert_not_called()
    assert workflow.calls == []


def test_direction_changed_after_capture_cannot_replace_captured_facts(workflow, monkeypatch):
    async def navigate(*args, **kwargs):
        set_resume(workflow, "222")
        monkeypatch.setattr(cr.config, "ACTIVE_DIRECTION", {"name": "electric"})
    monkeypatch.setattr(workflow.client._page, "goto", navigate)
    with pytest.raises(RuntimeError, match="ownership changed"):
        asyncio.run(workflow.run())
    assert "model" not in workflow.calls and "send" not in workflow.calls


def test_saved_draft_is_bound_to_the_same_application_resume(workflow):
    w = workflow
    preview = asyncio.run(w.run(dry_run=True))
    direction = Path(w.paths.home_dir) / "directions" / "qa.json"
    value = json.loads(direction.read_text())
    value["variants"].append({"hh_resume_numeric_id": "112"})
    direction.write_text(json.dumps(value))
    set_resume(w, "112")
    result = asyncio.run(cr.process_one(w.client, "chat", message_id="m1~" + preview["draft_revision"],
                                        dry_run=False, runtime_paths=w.paths))
    assert result["reason"] == "stale_draft"
    assert w.calls.count("model") == 1 and "send" not in w.calls
