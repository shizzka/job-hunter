"""Synthetic directions and HH observations for response view tests."""
import json

import directions


def make_direction(home, name="qa", identity=111, query="QA Engineer"):
    value = {
        "name": name, "title": query, "filter_policy": "generic", "search_queries": [query],
        "relevant_keywords": [], "exclude_keywords": [], "selection_strategy": "single",
        "variants": [{"hh_resume_hash": f"resume_{identity}", "hh_resume_numeric_id": identity,
                      "title": query}],
    }
    return directions.save_direction(str(home), value, texts={f"resume_{identity}": f"Synthetic resume {identity}"})


def two_directions(home):
    make_direction(home)
    make_direction(home, "electric", 222, "Электромонтажник")
    directions.set_active_direction(str(home), "qa")


def write_rows(home, rows):
    (home / "negotiation_snapshots.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def snapshot(name, section, resumes, *, complete=True, day=1):
    rows = [{"snapshot_id": name, "filter": section, "id": i, "chatId": i,
             "resumeId": resume, "fetched_at": f"2026-10-{day:02}T12:00:00+03:00"}
            for i, resume in enumerate(resumes, 1)]
    return rows + [{"kind": "snapshot_summary", "snapshot_id": name, "filter": section,
                    "complete": complete, "total": len(resumes) if complete else len(resumes) + 5,
                    "pages_read": 1, "fetched_at": f"2026-10-{day:02}T12:00:00+03:00",
                    "stop_reason": None if complete else "max_pages"}]
