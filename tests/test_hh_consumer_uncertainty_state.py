"""Late owned uncertainty remains terminal in consumer state and retry history."""
import pytest

import config
import hh_resume_pipeline as pipeline
import seen


def test_guard_stop_can_be_promoted_but_uncertain_cannot_be_downgraded(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SEEN_VACANCIES_FILE", str(tmp_path / "seen.json"))
    vacancy = {"id": "4", "title": "Synthetic", "company": "Synthetic"}
    seen.mark_seen("4", vacancy, "manual_hh_guard_stop")
    seen.mark_seen("4", vacancy, "apply_uncertain")
    for stale_action in ("manual_hh_guard_stop", "applied", "apply_failed:late", "already_applied"):
        with pytest.raises(RuntimeError):
            seen.mark_seen("4", vacancy, stale_action)
    assert seen.all_entries()["4"]["action"] == "apply_uncertain"
    assert seen.is_seen("4")
    assert seen.stats()["applied"] == 0 and seen.stats()["manual"] == 1


def test_retry_history_cannot_clear_late_uncertainty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HH_RESUME_PIPELINE_FILE", str(tmp_path / "pipeline.json"))
    monkeypatch.setattr(config, "HH_RESUME_PIPELINE_ENABLED", True)
    monkeypatch.setattr(config, "HH_PRIMARY_RESUME_ID", "synthetic-exact")
    monkeypatch.setattr(config, "HH_RESUME_RETRY_ON_SILENCE", True)
    vacancy = {"id": "4", "title": "Synthetic", "company": "Synthetic", "url": "https://hh.ru/vacancy/4"}
    variant = {"name": "normal", "id": "synthetic-exact"}
    pipeline.record_successful_apply(vacancy, variant)
    pipeline.mark_terminal("4", "manual_hh_guard_stop")
    pipeline.mark_terminal("4", "apply_uncertain")
    terminal = pipeline.all_entries()["4"]
    pipeline.mark_terminal("4", "already_applied")
    pipeline.record_successful_apply(vacancy, {"name": "alternative", "id": "synthetic-other"})
    pipeline.sync_negotiation_statuses([{**vacancy, "status": "Отказ"}])
    after = pipeline.all_entries()["4"]
    assert after["completed_reason"] == "apply_uncertain"
    assert after["attempts"] == terminal["attempts"]
    assert after["next_retry_at"] == "" and after["retry_reason"] == ""
    assert pipeline.get_retry_candidates() == []
