"""B18/B13: the «Ответ ИИ в чат» list skips closing messages and other profiles' chats."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import hh_chat_responder as cr
from hh.chat import ChatList
import directions
from tests.direction_view_fakes import make_direction, two_directions, write_rows


def test_candidate_list_filters_and_names(tmp_path, monkeypatch):
    import config
    home = tmp_path / "profiles" / "qa"
    make_direction(home)
    directions.set_active_direction(str(home), "qa")
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


def test_candidate_list_filters_direction_before_limit_and_keeps_unknown_for_review(tmp_path, monkeypatch):
    import config
    home = tmp_path / 'synthetic'
    two_directions(home)
    # The QA application exists only in the archive: B22 still binds it.
    write_rows(home, [
        {"chatId": 1, "resumeId": 222, "filter": "active"},
        {"chatId": 2, "resumeId": 111, "filter": "archived"},
    ])
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(home))
    monkeypatch.setattr(cr, 'load_state', lambda paths: {})
    monkeypatch.setattr(cr, 'list_chats', AsyncMock(return_value=ChatList(
        [{'chat_id': str(i), 'preview': 'Вопрос'} for i in (3, 1, 2)], list_complete=True)))

    async def messages(page, chat_id):
        return {'messages': [{'id': 'm' + chat_id, 'text': 'Какой у вас опыт?', 'is_me': False}],
                'vacancy': {'title': 'Synthetic ' + chat_id}}
    monkeypatch.setattr(cr, 'get_messages', messages)
    client = SimpleNamespace(_page=SimpleNamespace(goto=AsyncMock(), wait_for_timeout=AsyncMock()))
    summary = asyncio.run(cr.list_reply_candidates(client, limit=1, runtime_paths=SimpleNamespace()))
    assert [c['chat_id'] for c in summary['candidates']] == ['2']
    assert [c['chat_id'] for c in summary['ownership_reviews']] == ['3']
    assert summary['skipped_other_direction'] == 1
    from telegram_bot import _build_chat_ai_candidates_markup, _build_chat_ai_candidates_text
    text = _build_chat_ai_candidates_text(summary)
    assert 'QA Engineer' in text and 'Чат требует вашей проверки' in text
    markup = _build_chat_ai_candidates_markup('synthetic', summary, home_dir=str(home))
    callbacks = [b.get('callback_data', '') for r in markup['inline_keyboard'] for b in r]
    assert any(c.startswith('cbo:synthetic:') for c in callbacks)
    assert all(':3:' not in c for c in callbacks if c.startswith(('chat_ai:', 'chat_ai_any:', 'gform_preview:')))
    directions.set_active_direction(str(home), 'electric')
    summary = asyncio.run(cr.list_reply_candidates(client, limit=1, runtime_paths=SimpleNamespace()))
    assert [c['chat_id'] for c in summary['candidates']] == ['1']
    assert summary['direction_title'] == 'Электромонтажник'
