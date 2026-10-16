"""Synthetic observations only: fair scheduling, complete private data, one-shot diagnostics."""
import asyncio
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

from chat_observation import ChatJournal, diagnostic_snapshot, select_chat_batch


def rows(n=60):
    return [dict(chat_id=str(i), preview=f"Synthetic {i}") for i in range(n)]


def test_chat_rotation_covers_sixty_in_three_batches(tmp_path):
    batches = [select_chat_batch(tmp_path, rows(), 25) for _ in range(3)]
    assert all(len(batch) == 25 for batch in batches)
    assert {row["chat_id"] for batch in batches for row in batch} == {str(i) for i in range(60)}
    assert [row["chat_id"] for row in batches[1]][:2] == ["25", "26"]
    assert json.loads((tmp_path / "chat_scan_state.json").read_text())["next_chat_id"] == "15"


def test_chat_unread_and_changed_preview_first_without_permanent_priority(tmp_path):
    chats = rows()
    chats[45]["unread"] = True
    first = select_chat_batch(tmp_path, chats, 25)
    assert first[0]["chat_id"] == "45"
    # Change beyond the 300-char list preview must also be observed.
    chats[50].update(raw_text="A" * 350 + " New question", preview="A" * 300)
    second = select_chat_batch(tmp_path, chats, 25)
    assert second[0]["chat_id"] == "50"
    assert second[1]["chat_id"] == "24"
    third = select_chat_batch(tmp_path, chats, 25)
    assert third[0]["chat_id"] not in {"45", "50"}
    assert len({row["chat_id"] for batch in (first, second, third) for row in batch}) == 60


def test_chat_pending_priority_survives_budget_and_changing_order(tmp_path):
    chats = rows(8)
    for row in chats[4:]:
        row["unread"] = True
    assert select_chat_batch(tmp_path, chats, 2)[0]["chat_id"] == "4"
    second = select_chat_batch(tmp_path, list(reversed(chats)), 2)
    assert second[0]["chat_id"] == "7"
    assert second[1]["chat_id"] == "1"
    assert select_chat_batch(tmp_path, [], 25) == []


def test_chat_continuously_changing_priority_does_not_starve_tail(tmp_path):
    chats = rows(8)
    opened = set()
    for run in range(9):
        for row in chats:
            row["raw_text"] = f"Version {run}"
            row["unread"] = True
        opened.update(row["chat_id"] for row in select_chat_batch(tmp_path, chats, 2))
    assert opened == {str(i) for i in range(8)}


def test_chat_single_slot_fairness_and_profile_isolation(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    chats = rows(4)
    opened = set()
    for i in range(8):
        chats[3].update(unread=True, raw_text=str(i))
        opened.update(row["chat_id"] for row in select_chat_batch(a, chats, 1))
    assert opened == {"0", "1", "2", "3"}
    assert select_chat_batch(b, rows(4), 1)[0]["chat_id"] == "0"


def test_chat_journal_full_text_permissions_whitelist_and_parallel_records(tmp_path):
    journal = ChatJournal(tmp_path / "profile", "synthetic")
    text = "Full synthetic message\n" + "x" * 1800
    ref = journal.thread("42", {"history_complete": True, "cookies": "forbidden",
        "messages": [dict(id="1", text=text, raw_text=text + "\nHR", author="HR",
                          dom_index=0, controls=["Да", "Нет"], is_me=False,
                          classification_evidence=["synthetic"], time="12:00")], "vacancy": {}})
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda i: journal.write("chat_decision", chat_id=str(i), snapshot_ref=ref), range(40)))
    events = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert len(events) == 41
    thread = events[0]
    assert thread["messages"][0]["text"] == text
    assert thread["messages"][0]["raw_text"] == text + "\nHR"
    assert thread["messages"][0]["controls"] == ["Да", "Нет"]
    assert thread["history_complete"] is False
    assert "forbidden" not in journal.path.read_text()
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.home.stat().st_mode) == 0o700


def test_chat_diagnostics_are_one_shot_per_category_across_runs(tmp_path):
    journal = ChatJournal(tmp_path, "synthetic")
    page = SimpleNamespace(evaluate=AsyncMock(return_value="<div>Synthetic</div>"),
                           screenshot=AsyncMock(return_value=b"synthetic png"))
    async def run():
        await diagnostic_snapshot(journal, page, "42", "1", "hr_question", "ref", 0)
        assert not (tmp_path / "chat_diagnostics").exists()
        for decision in ("hr_question", "ours_last"):
            for i in range(3):
                await diagnostic_snapshot(ChatJournal(tmp_path, "synthetic"), page, "42", str(i), decision, "ref", 1)
    asyncio.run(run())
    root = tmp_path / "chat_diagnostics"
    assert page.screenshot.await_count == 2
    assert len(list(root.glob("*.html"))) == 2
    assert len(list(root.glob("*.png"))) == 2
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in root.iterdir())
