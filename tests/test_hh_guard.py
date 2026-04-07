import json
from datetime import datetime, timedelta, timezone

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
