"""Only application evidence or a thread-specific owner choice authorizes facts."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

import chat_context as cc
import chat_owner_review as review
import hh_chat_responder as cr
from tests.test_chat_workflow_transactions import workflow


def unknown(w):
    (Path(w.paths.home_dir) / "negotiation_snapshots.jsonl").unlink()


def selection(w, *, resume="111", direction="qa", chat_id="chat", actor=7):
    token = review.create_review(w.paths.home_dir, chat_id, "m1", "synthetic")
    return review.create_selection(w.paths.home_dir, token, [{
        "stage": "resume", "profile_name": "synthetic", "resume_id": resume,
        "direction": direction, "title": "Synthetic resume"}], actor)


@pytest.mark.parametrize("catalog_size", [1, 2])
def test_cached_account_resume_list_never_proves_application_owner(workflow, monkeypatch, catalog_size):
    w = workflow
    unknown(w)
    home = Path(w.paths.home_dir)
    if catalog_size == 1:
        (home / "directions" / "electric.json").unlink()
    # This exact cache format is also written by profile-filtered imports. Even
    # one unshared local resume cannot certify the whole HH account inventory.
    (home / "hh_resumes.json").write_text(json.dumps([
        {"id": str(index), "hh_resume_numeric_id": str(111 + index), "title": "Synthetic resume"}
        for index in range(catalog_size)]))
    from hh import negotiation_snapshot
    collect = Mock(side_effect=AssertionError("No reply-time HH reads"))
    monkeypatch.setattr(negotiation_snapshot, "collect", collect)
    assert cc.safe_resolve(w.paths.home_dir, "chat").status == cc.UNKNOWN
    assert asyncio.run(w.run())["reason"] == "unknown_owner"
    assert "model" not in w.calls and "send" not in w.calls
    collect.assert_not_called()


def test_single_resume_owner_choice_authorizes_only_selected_thread(workflow):
    w = workflow
    unknown(w)
    (Path(w.paths.home_dir) / "directions" / "electric.json").unlink()
    token = selection(w)
    review.confirm_selection(w.paths.home_dir, token, 0, 7, target_home=w.paths.home_dir)
    assert cc.resolve(w.paths.home_dir, "chat") == cc.ChatOwner(cc.OWN, "111", "qa", "Synthetic resume")
    assert cc.resolve(w.paths.home_dir, "another-thread").status == cc.UNKNOWN
    assert asyncio.run(w.run())["sent"]
    assert w.calls.count("model") == w.calls.count("click") == 1


def test_owner_choice_cannot_override_known_application_resume(workflow):
    w = workflow
    token = selection(w, resume="222", direction="electric")
    with pytest.raises(ValueError, match="другое резюме"):
        review.confirm_selection(w.paths.home_dir, token, 0, 7, target_home=w.paths.home_dir)
    assert review.confirmed_binding(w.paths.home_dir, "chat") is None
    assert cc.resolve(w.paths.home_dir, "chat").resume_id == "111"


def test_later_snapshot_conflict_stops_owner_selected_thread_before_model(workflow):
    w = workflow
    unknown(w)
    token = selection(w)
    review.confirm_selection(w.paths.home_dir, token, 0, 7, target_home=w.paths.home_dir)
    (Path(w.paths.home_dir) / "negotiation_snapshots.jsonl").write_text(
        json.dumps({"chatId": "chat", "resumeId": "222"}) + "\n")
    assert cc.resolve(w.paths.home_dir, "chat").status == cc.CONFLICT
    assert asyncio.run(w.run())["reason"] == "owner_conflict"
    assert "model" not in w.calls and "send" not in w.calls


def test_owner_selected_thread_is_rechecked_at_final_send(workflow, monkeypatch):
    w = workflow
    unknown(w)
    token = selection(w)
    review.confirm_selection(w.paths.home_dir, token, 0, 7, target_home=w.paths.home_dir)
    async def send(*args, **kwargs):
        (Path(w.paths.home_dir) / "negotiation_snapshots.jsonl").write_text(
            json.dumps({"chatId": "chat", "resumeId": "222"}) + "\n")
        await kwargs["before_send"]()
        pytest.fail("Conflicting resume reached click")
    monkeypatch.setattr(cr, "send_message", send)
    with pytest.raises(RuntimeError, match="ownership changed"):
        asyncio.run(w.run())
    assert "click" not in w.calls


def test_source_observations_are_rechecked_in_target_before_send(workflow, monkeypatch):
    w = workflow
    source = Path(w.paths.home_dir) / "source"
    token = review.create_review(source, "chat", "m1", "source")
    choice = review.create_selection(source, token, [{
        "stage": "resume", "profile_name": "synthetic", "resume_id": "111",
        "direction": "qa", "title": "Synthetic resume"}], 7)
    review.confirm_selection(source, choice, 0, 7, target_home=w.paths.home_dir)
    async def send(*args, **kwargs):
        (source / "negotiation_snapshots.jsonl").write_text(
            json.dumps({"chatId": "chat", "resumeId": "222"}) + "\n")
        await kwargs["before_send"]()
        pytest.fail("Source conflict reached click")
    monkeypatch.setattr(cr, "send_message", send)
    with pytest.raises(RuntimeError, match="ownership changed"):
        asyncio.run(w.run())
    assert "click" not in w.calls


def test_concurrent_owner_choices_cannot_overwrite_a_confirmed_binding(workflow):
    w = workflow
    unknown(w)
    tokens = [selection(w), selection(w, resume="222", direction="electric")]
    def confirm(token):
        try:
            return review.confirm_selection(w.paths.home_dir, token, 0, 7, target_home=w.paths.home_dir)
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, tokens))
    assert sum(result is not None for result in results) == 1
    assert cc.resolve(w.paths.home_dir, "chat").resume_id == next(
        result["resume_id"] for result in results if result)


def test_owner_selection_expires_and_does_not_create_a_binding(workflow, monkeypatch):
    w = workflow
    unknown(w)
    token = selection(w)
    now = review.time.time()
    monkeypatch.setattr(review.time, "time", lambda: now + review.SELECTION_AGE + 1)
    with pytest.raises(ValueError):
        review.confirm_selection(w.paths.home_dir, token, 0, 7, target_home=w.paths.home_dir)
    assert review.confirmed_binding(w.paths.home_dir, "chat") is None


def test_resolving_unknown_thread_does_not_create_profile_state(workflow):
    w = workflow
    unknown(w)
    home = Path(w.paths.home_dir)
    before = set(home.rglob("*"))
    assert cc.safe_resolve(str(home), "chat").status == cc.UNKNOWN
    assert set(home.rglob("*")) == before


def test_old_text_only_refusal_gets_an_owner_picker_once(workflow, monkeypatch):
    import notifier
    from unittest.mock import AsyncMock
    w = workflow
    unknown(w)
    with w.repo.attempt("chat", "notice", "ownership:m1") as attempt:
        assert w.repo.finish("chat", attempt, "completed")
    notice = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_message_with_markup", notice)
    for _ in range(2):
        assert asyncio.run(w.run_all())["answers_drafted"] == 0
    notice.assert_awaited_once()
    rows = notice.call_args.kwargs["reply_markup"]["inline_keyboard"]
    assert any(button.get("callback_data", "").startswith("cbo:synthetic:")
               for row in rows for button in row)
