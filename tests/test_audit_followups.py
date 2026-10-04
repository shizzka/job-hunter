"""Local A9/F4/F5/F6 safety regressions; no network or candidate data."""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import config
import hh_guard
import seen
import matcher
import agent
from tests.test_cover_grounding import isolated_candidate, approved, SENTENCES
from tests.test_matcher_deferred_safety import search_harness


@pytest.mark.parametrize('field,value', [('blocked_until', 'not-a-date'), ('blocked_until', 0),
    ('successful_apply_timestamps', ['not-a-date']), ('successful_apply_timestamps', 'not-a-list'),
    ('seeded_from_analytics_at', 'invalid'), ('last_detected_at', False)])
def test_semantic_guard_corruption_blocks_without_overwriting(tmp_path, monkeypatch, field, value):
    now = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    data = hh_guard._default_state()
    data['seeded_from_analytics_at'] = now.isoformat()
    data[field] = value
    path = tmp_path / 'guard.json'
    path.write_text(json.dumps(data))
    damaged = path.read_bytes()
    monkeypatch.setattr(config, 'HH_GUARD_STATE_FILE', str(path))
    for later in [now, now + timedelta(days=10)]:
        assert not hh_guard.can_auto_apply(now=later)[0]
        assert path.read_bytes() == damaged
    hh_guard.clear_cooldown(now=now)
    assert path.read_bytes() == damaged


@pytest.mark.parametrize('content', [b'{broken', b'[]', b'{"hh:1":false}'])
def test_corrupt_seen_never_makes_old_vacancy_new(tmp_path, monkeypatch, content):
    path = tmp_path / 'seen.json'
    path.write_bytes(content)
    monkeypatch.setattr(config, 'SEEN_VACANCIES_FILE', str(path))
    for _ in range(2):
        with pytest.raises(RuntimeError): seen.is_seen('hh:1')
        assert path.read_bytes() == content
    with pytest.raises(RuntimeError): seen.mark_seen('hh:2', {})
    assert path.read_bytes() == content


@pytest.mark.parametrize('finish', ['length', 'content_filter', 'tool_calls', None])
def test_incomplete_cover_cannot_be_accepted_by_optimistic_verifier(isolated_candidate, monkeypatch, finish):
    count = []
    async def create(**kwargs):
        count.append(True)
        text = ' '.join(SENTENCES) if len(count) == 1 else json.dumps(approved())
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish if len(count) == 1 else 'stop',
                    message=SimpleNamespace(content=text))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({'id': 'synthetic', 'title': 'QA'}))
    assert matcher.analyze_cover_letter(result)['fallback_cover_letter']
    assert len(count) == 1


def test_operational_search_logs_do_not_dump_cover_or_question_answers(search_harness, monkeypatch, caplog):
    private = 'SYNTHETIC_PRIVATE_CANDIDATE_PAYLOAD'
    client = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), is_logged_in=AsyncMock(return_value=True))
    monkeypatch.setattr(agent, 'HHClient', lambda: client)
    monkeypatch.setattr(config, 'HH_APPLICATION_MODE', 'auto')
    monkeypatch.setattr(config, 'HH_MIN_SECONDS_BETWEEN_APPLICATIONS', 0)
    monkeypatch.setattr(agent, 'evaluate_vacancy', AsyncMock(return_value={'score': 95, 'should_apply': True, 'reason': 'Match', 'red_flags': []}))
    monkeypatch.setattr(agent, 'generate_cover_letter', AsyncMock(return_value=private))
    dispatch = AsyncMock(return_value={'ok': False, 'requires_questions': True, 'message': 'Questions need review',
                'question_answers': [{'question': 'Identity?', 'answer': private}]})
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    monkeypatch.setattr(agent, 'notify_search_started', AsyncMock())
    monkeypatch.setattr(agent, 'notify_needs_manual', AsyncMock())
    monkeypatch.setattr(agent, 'create_task', lambda *a, **kw: None)
    with caplog.at_level(logging.INFO): asyncio.run(agent.do_search())
    dispatch.assert_awaited_once()
    assert private not in caplog.text


def test_chat_verification_logs_metadata_without_message_text(caplog):
    from hh.chat import send_message
    private = 'SYNTHETIC_PRIVATE_CHAT_BODY'
    page = SimpleNamespace(query_selector=AsyncMock(return_value=SimpleNamespace(click=AsyncMock())), wait_for_timeout=AsyncMock())
    fill = AsyncMock(return_value={'filled': True})
    extract = AsyncMock(return_value={'messages': [{'is_me': False, 'author': private, 'text': private}]})
    with caplog.at_level(logging.WARNING):
        assert not asyncio.run(send_message(page, 'synthetic', 'Approved', fill_preview=fill,
                                    extract_current_messages=extract))
    assert private not in caplog.text
