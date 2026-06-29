import manual_apply_queue


def test_manual_apply_queue_roundtrip(tmp_path, monkeypatch):
    queue_file = tmp_path / "manual_apply_queue.json"
    monkeypatch.setattr(manual_apply_queue.config, "MANUAL_APPLY_QUEUE_FILE", str(queue_file), raising=False)

    item = manual_apply_queue.create_candidate(
        {
            "id": "123",
            "source": "hh",
            "source_label": "hh.ru",
            "title": "QA Engineer",
            "company": "Acme",
            "salary": "100 000 ₽",
            "url": "https://hh.ru/vacancy/123",
            "snippet": "Manual QA, API",
        },
        {
            "score": 57,
            "reason": "Пограничное совпадение",
            "should_apply": False,
            "red_flags": [],
            "guard_flags": ["below_auto_apply_threshold"],
        },
        "Полное описание",
        profile_name="qa",
    )

    token = item["token"]
    assert manual_apply_queue.get_candidate(token)["vacancy"]["id"] == "123"

    callback_data = manual_apply_queue.manual_apply_callback_data("qa", token)
    assert callback_data == f"manual_apply:qa:{token}"
    assert manual_apply_queue.parse_manual_apply_callback_data(callback_data) == ("qa", token)

    markup = manual_apply_queue.build_manual_apply_markup(item["vacancy"], "qa", token)
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    assert any(button.get("url") == "https://hh.ru/vacancy/123" for button in buttons)
    assert any(button.get("callback_data") == callback_data for button in buttons)
    assert any(button.get("callback_data") == f"manual_fb:qa:{token}:good" for button in buttons)
    assert any(button.get("callback_data") == f"manual_fb:qa:{token}:bad" for button in buttons)
    assert manual_apply_queue.parse_manual_feedback_callback_data(f"manual_fb:qa:{token}:good") == ("qa", token, "good")

    compact_markup = manual_apply_queue.build_manual_apply_markup(
        item["vacancy"], "qa", token, include_feedback=False,
    )
    compact_buttons = [button for row in compact_markup["inline_keyboard"] for button in row]
    assert not any(str(button.get("callback_data", "")).startswith("manual_fb:") for button in compact_buttons)

    feedback = manual_apply_queue.record_feedback(token, "good", user_id=42)
    assert feedback["status"] == "pending"
    assert feedback["feedback"] == "good"
    assert feedback["feedback_label"] == "норм"
    assert feedback["feedback_user_id"] == 42

    feedback = manual_apply_queue.record_feedback(token, "bad", user_id=42)
    assert feedback["status"] == "dismissed"
    assert feedback["feedback"] == "bad"
    assert feedback["feedback_label"] == "мимо"

    updated = manual_apply_queue.mark_candidate(token, "applied", "ok")
    assert updated["status"] == "applied"
    assert manual_apply_queue.get_candidate(token)["message"] == "ok"
