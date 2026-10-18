import json
from types import SimpleNamespace

import manual_apply_queue as q


def test_every_rating_is_kept_in_a_durable_log(tmp_path, monkeypatch):
    monkeypatch.setattr(q, "_queue_path", lambda profile_name=None: tmp_path / "manual_apply_queue.json")
    vacancy = {"id": "7", "title": "Электромонтажник", "company": "A", "url": "https://hh.ru/vacancy/7",
               "_search_query": "Электромонтажник", "_direction_selection": {"direction": "d1", "direction_title": "Электрик"}}
    item = q.create_candidate(vacancy, {"score": 72, "reason": "ok"}, "", profile_name="p")
    q.record_feedback(item["token"], "good", user_id=1, profile_name="p")
    q.record_feedback(item["token"], "bad", user_id=1, profile_name="p")
    rows = [json.loads(line) for line in (tmp_path / "feedback_log.jsonl").read_text().splitlines()]
    assert [r["feedback"] for r in rows] == ["good", "bad"]
    assert rows[0]["vacancy_id"] == "7" and rows[0]["score"] == 72 and rows[0]["direction_title"] == "Электрик"
    assert rows[1]["status"] == "dismissed"
