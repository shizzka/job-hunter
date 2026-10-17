import csv
import io

from direction_stats import export_csv, summarize
from directions import save_direction


def direction(home, name, numeric_id, title="Same resume title"):
    return save_direction(home, {"name": name, "title": name,
        "filter_policy": "generic", "search_queries": [name],
        "relevant_keywords": [], "exclude_keywords": [], "selection_strategy": "single",
        "variants": [{"hh_resume_hash": name + "-hash", "title": title,
                      "hh_resume_numeric_id": numeric_id}]}, texts={name + "-hash": "# " + title})


def topic(identity, resume_id, **fields):
    return {"id": identity, "resumeId": resume_id, "fetched_at": "2026-10-17T12:00:00+03:00",
            "initialTopicType": "RESPONSE_BY_APPLICANT", "lastState": "RESPONSE", **fields}


def test_latest_numeric_observations_no_titles_attempts_or_double_counts(tmp_path):
    direction(tmp_path, "qa", 101)
    direction(tmp_path, "analyst", 102)
    snapshots = [topic(1, 101, fetched_at="2026-10-17T13:00:00+03:00", viewedByOpponent=True,
                       lastState="DISCARD"),
                 topic(1, 101),  # Older file position does not beat observation time.
                 topic(2, "102", lastEmployerState="INVITATION"),
                 topic(3, 101, viewedByOpponent=True),
                 topic(4, None, title="Same resume title"),
                 {"kind": "snapshot_summary", "total": 1000},
                 {"id": 999, "vacancyId": 900, "resumeId": 101, "kind": "local_apply_attempt"},
                 {"resumeId": 101}]
    result = summarize(tmp_path, snapshots)
    rows = {row["direction"]: row for row in result["rows"]}
    assert rows["qa"]["topics"] == 2
    assert {key: rows["qa"][key] for key in ("applied", "viewed", "rejected", "next_stage", "no_response")} == {
        "applied": 2, "viewed": 2, "rejected": 1, "next_stage": 0, "no_response": 1}
    assert rows["analyst"]["next_stage"] == 1
    assert result["unmatched"]["topics"] == 1
    assert result["total_topics"] == 4
    assert result["invalid_topics"] == 1


def test_reused_numeric_id_is_ambiguous_not_guessed(tmp_path):
    direction(tmp_path, "qa", 101)
    direction(tmp_path, "analyst", 101)
    result = summarize(tmp_path, [topic(1, 101), topic(2, 999)])
    assert all(row["topics"] == 0 for row in result["rows"])
    assert result["ambiguous"]["topics"] == 1
    assert result["ambiguous_resume_ids"] == [101]
    assert result["unmatched"]["topics"] == 1
    rows = list(csv.DictReader(io.StringIO(export_csv(result))))
    assert [row["direction"] for row in rows][-2:] == ["unmatched", "ambiguous"]


def test_default_snapshot_path_and_no_writes(tmp_path):
    direction(tmp_path, "qa", 101)
    journal = tmp_path / "negotiation_snapshots.jsonl"
    journal.write_text('{"id": 1, "resumeId": 101, "lastState": "RESPONSE"}\n{"truncated":')
    previous = journal.read_bytes()
    result = summarize(tmp_path)
    assert result["rows"][0]["applied"] == 1
    assert journal.read_bytes() == previous


def test_applicant_declines_are_not_employer_rejections_or_pending_responses(tmp_path):
    direction(tmp_path, "qa", 101)
    result = summarize(tmp_path, [topic(1, 101, declinedByApplicant=True, lastState="DISCARD"),
                                  topic(2, 101, declinedByApplicant=True, lastSubState="DECLINED")])
    row = result["rows"][0]
    assert row["applied"] == 2
    assert row["rejected"] == 0
    assert row["no_response"] == 0


def test_incoming_invitation_never_establishes_an_applicant_application(tmp_path):
    direction(tmp_path, "qa", 101)
    result = summarize(tmp_path, [topic(1, 101, initialTopicType="INVITATION_BY_EMPLOYER",
                                       lastState="INVITATION"),
                                  topic(2, 101, initialTopicType="INVITATION", lastState="RESPONSE")])
    assert result["rows"][0]["applied"] == 0
    assert result["rows"][0]["no_response"] == 0


def test_resume_mismatch_detects_other_resume_in_topic(tmp_path):
    import json as _json
    import directions as _directions
    import direction_stats as _stats
    root = tmp_path / "directions" / "el"
    root.mkdir(parents=True)
    text = root / ("a" * 64 + ".md")
    text.write_text("x")
    value = {"name": "el", "title": "Электрика", "filter_policy": "generic", "search_queries": ["электрик"],
             "relevant_keywords": [], "exclude_keywords": [],
             "variants": [{"hh_resume_hash": "h1", "hh_resume_numeric_id": 11, "title": "Монтажник",
                           "text_file": str(text), "text_sha256": "a" * 64, "fetched_at": "2026-10-17T10:00:00+00:00"},
                          {"hh_resume_hash": "h2", "hh_resume_numeric_id": 22, "title": "Техник",
                           "text_file": str(text), "text_sha256": "a" * 64, "fetched_at": "2026-10-17T10:00:00+00:00"}]}
    events = tmp_path / "analytics_events.jsonl"
    events.write_text("\n".join(_json.dumps(e) for e in [
        {"event": "decision", "outcome": "applied", "vacancy_id": "100", "requested_resume_id": "h1", "title": "A", "company": "C"},
        {"event": "decision", "outcome": "applied", "vacancy_id": "200", "requested_resume_id": "h2", "title": "B", "company": "D"},
        {"event": "decision", "outcome": "skipped", "vacancy_id": "300", "requested_resume_id": "h1"},
    ]))
    snapshots = [{"id": 1, "vacancyId": 100, "resumeId": 22, "fetched_at": "2026-10-17T11:00:00"},
                 {"id": 2, "vacancyId": 200, "resumeId": 22, "fetched_at": "2026-10-17T11:00:00"},
                 {"id": 3, "vacancyId": 300, "resumeId": 22, "fetched_at": "2026-10-17T11:00:00"}]
    import unittest.mock as mock
    with mock.patch.object(_stats, "list_directions", lambda home: [_directions.validate_direction(value)]):
        result = _stats.resume_mismatches(tmp_path, snapshots=snapshots, events_path=events)
    assert [(r["vacancy_id"], r["requested_title"], r["actual_title"]) for r in result] == [("100", "Монтажник", "Техник")]
