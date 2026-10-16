"""Local snapshot resolution and human links; no external services."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import hh_chat_responder as responder
from hh.negotiation_links import NEGOTIATIONS_URL, chat_link, find_chat_negotiation


def snapshot(name='active-1', *, section='active', topic=5600000001,
             chat=5700000002, complete=True):
    return [
        {'snapshot_id': name, 'filter': section, 'id': topic,
         'chatId': chat, 'vacancyId': 130000001},
        {'kind': 'snapshot_summary', 'snapshot_id': name, 'filter': section,
         'complete': complete, 'total': 1},
    ]


def write(home, records):
    home.mkdir(parents=True, exist_ok=True)
    (home / 'negotiation_snapshots.jsonl').write_text(
        ''.join(json.dumps(row) + '\n' for row in records), encoding='utf-8')


def test_chat_topic_and_vacancy_from_latest_complete_sections(tmp_path):
    records = snapshot(topic=1)
    records += snapshot('active-2')
    records += snapshot('partial', topic=3, complete=False)
    records += snapshot('archive', section='archived', chat=42, topic=43)
    write(tmp_path, records)
    assert find_chat_negotiation(tmp_path, '5700000002') == {
        'topic_id': '5600000001', 'vacancy_id': '130000001'}
    assert find_chat_negotiation(tmp_path, 42)['topic_id'] == '43'
    assert chat_link(tmp_path, 42) == {
        'text': 'Открыть чат', 'url': NEGOTIATIONS_URL + '/item?topicId=43'}


def test_chat_does_not_use_record_from_superseded_snapshot(tmp_path):
    write(tmp_path, snapshot() + snapshot('new', chat=99))
    assert find_chat_negotiation(tmp_path, 5700000002) is None


@pytest.mark.parametrize('journal', ['missing', 'no_match', 'partial', 'corrupt', 'invalid_utf8', 'invalid_record'])
def test_chat_fallback_for_missing_or_invalid_snapshot(tmp_path, journal):
    if journal != 'missing':
        write(tmp_path, snapshot(complete=journal != 'partial'))
    path = tmp_path / 'negotiation_snapshots.jsonl'
    if journal == 'corrupt':
        with path.open('a') as stream:
            stream.write('{broken\n')
    elif journal == 'invalid_utf8':
        with path.open('ab') as stream:
            stream.write(b'\xff\n')
    elif journal == 'invalid_record':
        write(tmp_path, [None])
    assert chat_link(tmp_path, 99 if journal == 'no_match' else 5700000002) == {
        'text': 'Открыть отклики', 'url': NEGOTIATIONS_URL}


def test_chat_latest_empty_snapshot_replaces_old_records(tmp_path):
    write(tmp_path, snapshot() + [{
        'kind': 'snapshot_summary', 'snapshot_id': 'empty', 'filter': 'active',
        'complete': True, 'total': 0}])
    assert find_chat_negotiation(tmp_path, 5700000002) is None


@pytest.mark.parametrize('found', [True, False])
def test_chat_all_human_link_surfaces(tmp_path, monkeypatch, found):
    monkeypatch.setattr(responder.config, 'JOB_HUNTER_HOME', str(tmp_path))
    monkeypatch.setattr(responder, '_active_profile_name', lambda: 'qa')
    if found:
        write(tmp_path, snapshot())
    chat_id = '5700000002'
    expected = chat_link(tmp_path, chat_id)
    anchor = f"<a href='{expected['url']}'>{expected['text']}</a>"
    assert responder.build_suspicious_chat_reply_markup('qa', chat_id, 'm')['inline_keyboard'][0] == [expected]
    assert responder.build_chat_answer_preview_markup('qa', chat_id, 'm')['inline_keyboard'][0] == [expected]
    assert anchor in responder._sent_caption(chat_id, {}, {}, 'answer')
    assert anchor in responder._chat_result_caption({'chat_id': chat_id, 'profile_name': 'qa'})

    class Notifier:
        async def send_message_with_markup(self, text, **kwargs):
            self.text, self.kwargs = text, kwargs
            return True

    notifier = Notifier()
    asyncio.run(responder.notify_hr_message(
        notifier, chat_id=chat_id, vacancy={}, message={}, profile_name='qa'))
    assert notifier.kwargs['reply_markup']['inline_keyboard'][0] == [expected]
    asyncio.run(responder._notify_google_form_failure(
        notifier, chat_id=chat_id, vacancy={}, message={}, form_url='https://example.org', error='failed'))
    assert anchor in notifier.text


def test_chat_link_reads_requested_profile_without_activation(tmp_path, monkeypatch):
    import profile
    home = tmp_path / 'requested'
    write(home, snapshot())
    monkeypatch.setattr(responder, '_active_profile_name', lambda: 'other')
    monkeypatch.setattr(profile, 'load_profile', lambda name: SimpleNamespace(home_dir=home)
                        if name == 'qa' else pytest.fail('wrong profile'))
    assert responder.build_chat_answer_preview_markup('qa', '5700000002', 'm')['inline_keyboard'][0] == [
        chat_link(home, '5700000002')]
