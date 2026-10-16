"""Resolve human-facing chat links from local negotiation snapshots only."""
from __future__ import annotations

import json
from pathlib import Path


NEGOTIATIONS_URL = 'https://hh.ru/applicant/negotiations'


def _id(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return value.lstrip('0') or None
    return None


def find_chat_negotiation(home_dir, chat_id):
    """Return topic/vacancy IDs from the latest complete active/archive passes.

    File order determines recency. Partial passes never replace complete ones;
    corrupt or unreadable journals fail closed to the negotiations list.
    """
    wanted = _id(chat_id)
    if wanted is None:
        return None
    pending, latest = {}, {}
    try:
        with (Path(home_dir) / 'negotiation_snapshots.jsonl').open(encoding='utf-8') as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    return None
                snapshot_id = record.get('snapshot_id')
                section = record.get('filter')
                if not isinstance(snapshot_id, str) or section not in ('active', 'archived'):
                    return None
                key = (snapshot_id, section)
                if record.get('kind') == 'snapshot_summary':
                    rows = pending.pop(key, [])
                    if record.get('complete') is True:
                        if type(record.get('total')) is not int or record['total'] != len(rows):
                            return None
                        # Reinsert so reverse iteration prefers the newest section.
                        latest.pop(section, None)
                        latest[section] = rows
                else:
                    pending.setdefault(key, []).append(record)
    except (OSError, ValueError, TypeError):
        return None
    for rows in reversed(list(latest.values())):
        for row in rows:
            topic_id = _id(row.get('id'))
            if _id(row.get('chatId')) == wanted and topic_id:
                return {'topic_id': topic_id, 'vacancy_id': _id(row.get('vacancyId'))}
    return None


def chat_link(home_dir, chat_id):
    """Return the button label and URL supported by the HH mobile app."""
    negotiation = find_chat_negotiation(home_dir, chat_id)
    if negotiation:
        return {'text': 'Открыть чат',
                'url': NEGOTIATIONS_URL + '/item?topicId=' + negotiation['topic_id']}
    return {'text': 'Открыть отклики', 'url': NEGOTIATIONS_URL}
