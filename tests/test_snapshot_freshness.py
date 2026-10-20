import json
from datetime import datetime, timedelta

from hh import negotiation_snapshot as ns


def write_summary(path, hours_ago, *, filter_name="active", complete=True):
    when = (datetime.now().astimezone() - timedelta(hours=hours_ago)).isoformat()
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "snapshot_summary", "filter": filter_name, "complete": complete,
                                 "fetched_at": when}) + "\n")


def test_ensure_fresh_collects_only_when_missing_or_stale(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ns, "collect", lambda **kw: calls.append(kw) or {"complete": True})
    assert ns.ensure_fresh(str(tmp_path), "c.json", max_age_hours=6) == {"complete": True}  # missing
    path = tmp_path / "negotiation_snapshots.jsonl"
    write_summary(path, 1)
    assert ns.ensure_fresh(str(tmp_path), "c.json", max_age_hours=6) is None  # fresh
    write_summary(path, 0.5, complete=False)  # an incomplete pass does not count
    write_summary(path, 0.2, filter_name="archived")  # another filter does not count
    assert ns.latest_complete_age_hours(str(tmp_path)) < 2
    path.write_text("")
    write_summary(path, 10)
    ns.ensure_fresh(str(tmp_path), "c.json", max_age_hours=6)
    assert len(calls) == 2 and calls[-1]["archived"] is False


def test_chat_refresh_bounds_both_filters_and_finds_archived_owner(tmp_path, monkeypatch):
    import chat_context as cc
    home = tmp_path / 'profiles' / 'synthetic'
    (home / 'directions').mkdir(parents=True)
    (home / 'directions' / 'qa.json').write_text(json.dumps({
        'name': 'qa', 'variants': [{'hh_resume_hash': 'a' * 38,
                                   'hh_resume_numeric_id': 111, 'title': 'QA'}]}))
    calls = []
    def collect(**kwargs):
        calls.append(kwargs)
        section = 'archived' if kwargs['archived'] else 'active'
        rows = ([{'snapshot_id': section, 'filter': section, 'chatId': 42,
                  'resumeId': 111, 'id': 123}] if kwargs['archived'] else [])
        summary = {'kind': 'snapshot_summary', 'snapshot_id': section,
                   'filter': section, 'total': len(rows), 'complete': True,
                   'stop_reason': None}
        with (home / 'negotiation_snapshots.jsonl').open('a') as stream:
            for row in rows + [summary]:
                stream.write(json.dumps(row) + '\n')
        return summary
    monkeypatch.setattr(ns, 'collect', collect)
    assert cc.safe_resolve(str(home), '42').status == cc.UNKNOWN
    results = ns.refresh_for_chats(home, 'synthetic-cookies', now=lambda: 1000)
    assert [item['filter'] for item in results] == ['active', 'archived']
    assert [(item['archived'], item['max_pages']) for item in calls] == [(False, 3), (True, 3)]
    assert cc.safe_resolve(str(home), '42').status == cc.OWN


def test_chat_refresh_partial_attempts_are_durably_throttled(tmp_path, monkeypatch):
    calls = []
    def collect(**kwargs):
        calls.append(kwargs['archived'])
        return {'complete': False, 'stop_reason': 'max_pages'}
    monkeypatch.setattr(ns, 'collect', collect)
    ns.refresh_for_chats(tmp_path, 'synthetic', now=lambda: 1000)
    assert calls == [False, True]
    # Each call constructs a new store; no in-memory cooldown is involved.
    results = ns.refresh_for_chats(tmp_path, 'synthetic', now=lambda: 1001)
    assert calls == [False, True]
    assert all(item['stop_reason'] == 'refresh_throttled' for item in results)
    ns.refresh_for_chats(tmp_path, 'synthetic', now=lambda: 1000 + 6 * 3600)
    assert calls == [False, True, False, True]


def test_chat_refresh_failed_or_crashed_active_never_starts_archive(tmp_path, monkeypatch):
    import pytest
    for reason in ('antibot_captcha', 'guard_blocked', 'http_403', 'collection_error'):
        home = tmp_path / reason
        calls = []
        def collect(**kwargs):
            calls.append(kwargs['archived'])
            return {'complete': False, 'stop_reason': reason}
        monkeypatch.setattr(ns, 'collect', collect)
        ns.refresh_for_chats(home, 'synthetic', now=lambda: 1000)
        ns.refresh_for_chats(home, 'synthetic', now=lambda: 1001)
        assert calls == [False]
    home = tmp_path / 'crash'
    def crash(**kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(ns, 'collect', crash)
    with pytest.raises(KeyboardInterrupt):
        ns.refresh_for_chats(home, 'synthetic', now=lambda: 1000)
    monkeypatch.setattr(ns, 'collect', lambda **kwargs: pytest.fail('crash lost reservation'))
    ns.refresh_for_chats(home, 'synthetic', now=lambda: 1001)


def test_chat_refresh_guard_stops_before_reservation(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(ns, 'guard_stop_reason', lambda home: 'guard_blocked')
    monkeypatch.setattr(ns, 'collect', lambda **kwargs: pytest.fail('guard bypass'))
    assert ns.refresh_for_chats(tmp_path, 'synthetic')[0]['stop_reason'] == 'guard_blocked'
    assert not (tmp_path / 'negotiation_refresh_state.json').exists()


def test_chat_refresh_concurrent_caller_cannot_start_archive(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release = Event(), Event()
    calls = []
    def collect(**kwargs):
        calls.append(kwargs['archived'])
        if not kwargs['archived']:
            entered.set()
            assert release.wait(5)
        return {'complete': True, 'stop_reason': None}
    monkeypatch.setattr(ns, 'collect', collect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ns.refresh_for_chats, tmp_path, 'synthetic', now=lambda: 1000)
        assert entered.wait(5)
        try:
            second = pool.submit(ns.refresh_for_chats, tmp_path, 'synthetic', now=lambda: 1001).result(5)
            assert all(item['stop_reason'] == 'refresh_throttled' for item in second)
            assert calls == [False]
        finally:
            release.set()
        first.result(5)
    assert calls == [False, True]


def test_chat_refresh_rejects_corrupt_throttle_state(tmp_path, monkeypatch):
    import pytest
    path = tmp_path / 'negotiation_refresh_state.json'
    monkeypatch.setattr(ns, 'collect', lambda **kwargs: pytest.fail('corrupt state bypass'))
    for content in ('broken', '{}', '[]', '{"schema_version": 1, "attempted_at": {"active": "bad"}}'):
        path.write_text(content)
        with pytest.raises(RuntimeError):
            ns.refresh_for_chats(tmp_path, 'synthetic')
        assert path.read_text() == content


def test_chat_refresh_skips_legacy_complete_passes(tmp_path, monkeypatch):
    import pytest
    path = tmp_path / 'negotiation_snapshots.jsonl'
    for section in ('active', 'archived'):
        write_summary(path, 1, filter_name=section)
    monkeypatch.setattr(ns, 'collect', lambda **kwargs: pytest.fail('fresh pass repeated'))
    results = ns.refresh_for_chats(tmp_path, 'synthetic')
    assert all(item['complete'] and item['skipped'] == 'recent_complete' for item in results)
