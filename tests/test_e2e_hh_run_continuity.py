"""Offline Chromium gate: one vacancy 403 must leave later HH work runnable."""
import asyncio

from tests.test_e2e_hh_apply_happy_path import NOISE_URLS, run_hh_gate


def test_hh_vacancy_forbidden_preserves_run_continuity(tmp_path, monkeypatch):
    gate = asyncio.run(run_hh_gate(tmp_path, monkeypatch, continuity=True))
    # Verify the first real navigation received 403 and the departing live
    # document emitted real pagehide POSTs, all terminated by offline routing.
    assert ("GET", "https://hh.ru/vacancy/1", 403) in gate.evidence["documents"]
    assert set(gate.evidence["noise_posts"]) == {("POST", url) for url in NOISE_URLS}
    assert all(("POST", url) in gate.answered for url in NOISE_URLS)
    first = [event for event in gate.decisions if event.get("vacancy_id") == "1"]
    assert len(first) == 1, gate.evidence
    assert first[0]["decision"] in {"skipped", "unavailable", "skipped_unavailable"}, gate.evidence
    assert first[0]["reason_code"] == "vacancy_forbidden", gate.evidence
    assert gate.evidence["matched"] == ["2", "3"], gate.evidence
    assert all(gate.entries.get(vid, {}).get("action") == "skipped_low_score" for vid in ("2", "3")), gate.evidence
    assert all(any(event.get("vacancy_id") == vid and event["decision"] == "skipped_low_score"
                   for event in gate.decisions) for vid in ("2", "3")), gate.evidence
    assert gate.result["funnel"]["not_processed"] == 0, gate.evidence
    assert gate.result["hh_recovery"]["stopped"] is False, gate.evidence
