import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
import hh_guard


def _dt(hour: int) -> datetime:
    return datetime(2026, 3, 27, hour, 0, tzinfo=timezone(timedelta(hours=3)))


def test_detect_antibot_kind_variants():
    assert hh_guard.detect_antibot_kind("DDOS-GUARD Проверка браузера перед переходом на hh.ru") == "ddos_guard"
    assert hh_guard.detect_antibot_kind("hh.ru anti-bot (captcha) после отклика") == "captcha"
    assert hh_guard.detect_antibot_kind("hh.ru anti-bot (rate limit)") == "rate_limit"


def test_hh_guard_limits_auto_apply_over_24h(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HH_GUARD_STATE_FILE", str(tmp_path / "hh_guard_state.json"))
    monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", str(tmp_path / "analytics_events.jsonl"))
    monkeypatch.setattr(config, "HH_AUTO_APPLY_MAX_PER_24H", 2)

    hh_guard.record_apply_success(now=_dt(10))
    hh_guard.record_apply_success(now=_dt(11))

    ok, note = hh_guard.can_auto_apply(now=_dt(12))

    assert ok is False
    assert "2/2" in note


def test_hh_guard_blocks_collection_after_antibot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HH_GUARD_STATE_FILE", str(tmp_path / "hh_guard_state.json"))
    monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", str(tmp_path / "analytics_events.jsonl"))
    monkeypatch.setattr(config, "HH_ANTI_BOT_COOLDOWN_HOURS", 6)

    status = hh_guard.record_antibot(
        kind="ddos_guard",
        raw_message="DDOS-GUARD",
        stage="search",
        now=_dt(7),
    )

    can_collect, collect_note = hh_guard.can_collect(now=_dt(8))
    can_apply, apply_note = hh_guard.can_auto_apply(now=_dt(8))
    can_collect_after, _ = hh_guard.can_collect(now=_dt(14))

    assert status["blocked"] is True
    assert can_collect is False
    assert can_apply is False
    assert "DDOS-GUARD" in collect_note
    assert "DDOS-GUARD" in apply_note
    assert can_collect_after is True


def test_hh_guard_bootstraps_from_analytics(tmp_path, monkeypatch):
    guard_file = tmp_path / "hh_guard_state.json"
    analytics_file = tmp_path / "analytics_events.jsonl"
    monkeypatch.setattr(config, "HH_GUARD_STATE_FILE", str(guard_file))
    monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", str(analytics_file))
    monkeypatch.setattr(config, "HH_AUTO_APPLY_MAX_PER_24H", 1)

    analytics_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "decision",
                        "created_at": _dt(9).isoformat(timespec="seconds"),
                        "source": "hh",
                        "decision": "applied_auto",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "event": "decision",
                        "created_at": _dt(9).isoformat(timespec="seconds"),
                        "source": "superjob",
                        "decision": "applied_auto",
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding="utf-8",
    )

    ok, note = hh_guard.can_auto_apply(now=_dt(12))
    status = hh_guard.get_status(now=_dt(12))

    assert ok is False
    assert "1/1" in note
    assert status["rolling_apply_count_24h"] == 1


def test_hh_guard_fails_closed_and_preserves_corrupt_state(tmp_path, monkeypatch):
    guard_file = tmp_path / "hh_guard_state.json"
    guard_file.write_text("{truncated", encoding="utf-8")
    monkeypatch.setattr(config, "HH_GUARD_STATE_FILE", str(guard_file))
    monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", str(tmp_path / "analytics_events.jsonl"))
    monkeypatch.setattr(config, "HH_ANTI_BOT_COOLDOWN_HOURS", 6)

    ok, note = hh_guard.can_auto_apply(now=_dt(8))
    persisted = json.loads(guard_file.read_text(encoding="utf-8"))

    assert ok is False
    assert "state corruption" in note
    assert persisted["last_kind"] == "state_corruption"
    backups = list(tmp_path.glob("hh_guard_state.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{truncated"


def _isolate_guard(tmp_path, monkeypatch):
    guard_file = tmp_path / "hh_guard_state.json"
    analytics_file = tmp_path / "analytics_events.jsonl"
    monkeypatch.setattr(config, "HH_GUARD_STATE_FILE", str(guard_file))
    monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", str(analytics_file))
    return guard_file, analytics_file


def _apply_event(now):
    return json.dumps({"event": "decision", "created_at": now.isoformat(), "source": "hh", "decision": "applied_auto"}) + "\n"


@pytest.mark.parametrize("log_content", [None, "", _apply_event(_dt(9) - timedelta(days=2))])
def test_empty_seed_is_persisted_and_not_repeated(tmp_path, monkeypatch, log_content):
    guard_file, analytics_file = _isolate_guard(tmp_path, monkeypatch)
    if log_content is not None:
        analytics_file.write_text(log_content)
    original_seed = hh_guard._seed_apply_timestamps_from_analytics
    calls = []

    def counted_seed(now=None):
        calls.append(now)
        return original_seed(now)

    monkeypatch.setattr(hh_guard, "_seed_apply_timestamps_from_analytics", counted_seed)
    for _ in range(3):
        assert hh_guard.get_status(now=_dt(12))["rolling_apply_count_24h"] == 0
    hh_guard.record_soft_cooldown(now=_dt(12))
    hh_guard.clear_cooldown(now=_dt(12))
    assert len(calls) == 1
    persisted = json.loads(guard_file.read_text())
    assert datetime.fromisoformat(persisted["seeded_from_analytics_at"]) == _dt(12)


def test_empty_seed_survives_a_fresh_process(tmp_path, monkeypatch):
    guard_file, analytics_file = _isolate_guard(tmp_path, monkeypatch)
    hh_guard.get_status(now=_dt(12))
    script = """
import sys
from datetime import datetime
import config
import hh_guard
config.HH_GUARD_STATE_FILE = sys.argv[1]
config.ANALYTICS_EVENTS_FILE = sys.argv[2]
def unexpected_seed(**kwargs):
    raise AssertionError('Analytics must not be scanned again')
hh_guard._seed_apply_timestamps_from_analytics = unexpected_seed
state = hh_guard.get_status(now=datetime.fromisoformat(sys.argv[3]))
assert state['rolling_apply_count_24h'] == 0
assert not state['blocked']
print('PERSISTED_SEED_OK')
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(guard_file), str(analytics_file), _dt(12).isoformat()],
        cwd=Path(hh_guard.__file__).parent, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "PERSISTED_SEED_OK" in result.stdout


def test_expired_seed_does_not_rescan_and_new_success_is_counted(tmp_path, monkeypatch):
    _guard_file, analytics_file = _isolate_guard(tmp_path, monkeypatch)
    analytics_file.write_text(_apply_event(_dt(9)))
    assert hh_guard.get_status(now=_dt(12))["rolling_apply_count_24h"] == 1

    def unexpected_seed(**kwargs):
        pytest.fail("Expiration of rolling timestamps must not rescan analytics")

    monkeypatch.setattr(hh_guard, "_seed_apply_timestamps_from_analytics", unexpected_seed)
    next_day = _dt(12) + timedelta(days=1)
    assert hh_guard.get_status(now=next_day)["rolling_apply_count_24h"] == 0
    assert hh_guard.record_apply_success(now=next_day)["rolling_apply_count_24h"] == 1


def test_failed_analytics_read_is_not_cached_and_fails_closed(tmp_path, monkeypatch):
    import builtins

    guard_file, analytics_file = _isolate_guard(tmp_path, monkeypatch)
    analytics_file.write_text(_apply_event(_dt(9)))
    original_open = builtins.open

    def inaccessible(path, *args, **kwargs):
        if str(path) == str(analytics_file):
            raise PermissionError("Unreadable analytics")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", inaccessible)
        assert hh_guard.can_auto_apply(now=_dt(12))[0] is False
        assert not guard_file.exists()
    status = hh_guard.get_status(now=_dt(12))
    assert status["rolling_apply_count_24h"] == 1
    assert status["seeded_from_analytics_at"]


def test_seed_preserves_cooldown_written_between_load_and_update(tmp_path, monkeypatch):
    guard_file, analytics_file = _isolate_guard(tmp_path, monkeypatch)
    analytics_file.write_text(_apply_event(_dt(9)))
    store_factory = hh_guard._store

    class ConcurrentStore:
        def __init__(self, *args, **kwargs):
            self.store = store_factory(*args, **kwargs)

        def load(self):
            snapshot = self.store.load()

            def concurrent_update(state):
                state["blocked_until"] = _dt(18).isoformat()
                state["last_kind"] = "captcha"
                return state

            self.store.update(concurrent_update)
            return snapshot

        def update(self, mutator):
            return self.store.update(mutator)

    monkeypatch.setattr(hh_guard, "_store", ConcurrentStore)
    status = hh_guard.get_status(now=_dt(12))
    assert status["blocked"] is True
    assert status["last_kind"] == "captcha"
    assert status["rolling_apply_count_24h"] == 1
    assert datetime.fromisoformat(json.loads(guard_file.read_text())["blocked_until"]) == _dt(18)


@pytest.mark.parametrize('local_timezone', ['UTC', 'Europe/Moscow'])
def test_persisted_guard_timestamps_preserve_instant_across_local_timezones(tmp_path, monkeypatch, local_timezone):
    guard_file, _ = _isolate_guard(tmp_path, monkeypatch)
    try:
        with monkeypatch.context() as scoped:
            scoped.setenv('TZ', local_timezone)
            time.tzset()
            hh_guard.get_status(now=_dt(12))
            hh_guard.record_soft_cooldown(now=_dt(12), minutes=15)
            saved = json.loads(guard_file.read_text())
            assert datetime.fromisoformat(saved['seeded_from_analytics_at']) == _dt(12)
            assert datetime.fromisoformat(saved['blocked_until']) == _dt(12) + timedelta(minutes=15)
    finally:
        time.tzset()


def test_seed_skips_non_object_json_lines_but_keeps_valid_events(tmp_path, monkeypatch):
    _guard_file, analytics_file = _isolate_guard(tmp_path, monkeypatch)
    analytics_file.write_text('[]\nnull\n42\n"text"\n{invalid\n' + _apply_event(_dt(9)))
    assert hh_guard.get_status(now=_dt(12))["rolling_apply_count_24h"] == 1
