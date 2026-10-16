"""Best-effort metrics from markup already fetched by the action/details path."""
from datetime import datetime
from pathlib import Path

import config
from hh.collection_journal import append_records
from hh.observation import initial_state, _number


def record(html, vacancy_id, stage):
    """No requests, action evidence, or exceptions escaping to the caller."""
    try:
        try:
            state = initial_state(html)
        except ValueError:
            state = {}
        def obj(value):
            return value if isinstance(value, dict) else {}
        state = obj(state)
        extra = obj(obj(obj(state.get('vacancyView')).get('vacancyFull')).get('extraVacancyFields'))
        online = obj(obj(state.get('vacancyOnlineUsersCounters')).get(str(vacancy_id)))
        row = {
            'ts': datetime.now().astimezone().isoformat(timespec='microseconds'),
            'vacancy_id': _number(vacancy_id), 'stage': stage,
            'total_responses': _number(extra.get('totalResponsesCount')),
            'vacancy_responses': _number(extra.get('vacancyResponsesCount')),
            'online_including_current': _number(online.get('includingCurrent')),
            'online_excluding_current': _number(online.get('excludingCurrent')),
        }
        append_records(Path(config.JOB_HUNTER_HOME) / 'vacancy_metrics.jsonl', [row])
    except Exception:
        pass
