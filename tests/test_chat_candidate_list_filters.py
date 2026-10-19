"""B18/B13: the «Ответ ИИ в чат» list skips closing messages and other profiles' chats."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import hh_chat_responder as cr
from hh.chat import ChatList


def test_candidate_list_filters_and_names(tmp_path, monkeypatch):
    import config
    home = tmp_path / "profiles" / "qa"
    (home / "directions").mkdir(parents=True)
    (home / "directions" / "d.json").write_text(json.dumps({"name": "d", "title": "QA", "variants": [
        {"hh_resume_hash": "c" * 38, "hh_resume_numeric_id": 111, "title": "QA Engineer"}]}))
    rows = [{"chatId": 1, "resumeId": 111, "lastState": "RESPONSE", "company_name": "Acme", "vacancy_name": "Стажёр QA"},
            {"chatId": 2, "resumeId": 999, "lastState": "RESPONSE"},
            {"chatId": 3, "resumeId": 111, "lastState": "RESPONSE", "company_name": "Synthetic Corp", "vacancy_name": "QA"}]
    (home / "negotiation_snapshots.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(home))
    texts = {"1": "К сожалению, сейчас мы не готовы пригласить вас на следующий этап.",
             "2": "Какой у вас опыт с SQL?", "3": "Какой у вас опыт с Postman?"}
    monkeypatch.setattr(cr, "list_chats", AsyncMock(return_value=ChatList(
        [dict(chat_id=c, preview="Перейти") for c in texts], list_complete=True, stop_reason="stable_bottom")))

    async def messages(page, chat_id):
        return {"messages": [dict(id="m" + chat_id, text=texts[chat_id], is_me=False)],
                "vacancy": {"title": "Перейти", "company": "—"}}

    monkeypatch.setattr(cr, "get_messages", messages)
    monkeypatch.setattr(cr, "load_state", lambda paths: {})
    client = SimpleNamespace(_page=SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock()))
    summary = asyncio.run(cr.list_reply_candidates(client, limit=8, max_scan=10, runtime_paths=SimpleNamespace()))
    assert [c["chat_id"] for c in summary["candidates"]] == ["3"]
    assert summary["candidates"][0]["company"] == "Synthetic Corp" and summary["candidates"][0]["title"] == "QA"
    assert summary["skipped_closing"] == 1 and summary["skipped_other_profile"] == 1
