import manual_apply_queue


def test_manual_apply_queue_roundtrip(tmp_path, monkeypatch):
    queue_file = tmp_path / "manual_apply_queue.json"
    monkeypatch.setattr(manual_apply_queue, "_queue_path", lambda profile_name=None: queue_file)
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

    preview_markup = manual_apply_queue.build_manual_apply_markup(
        item["vacancy"], "qa", token, allow_ai_apply=False,
    )
    preview_buttons = [button for row in preview_markup["inline_keyboard"] for button in row]
    assert not any(button.get("callback_data") == callback_data for button in preview_buttons)
    assert any(button.get("url") == "https://hh.ru/vacancy/123" for button in preview_buttons)

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


def test_review_queue_is_profile_scoped_and_skips_snoozed_items(tmp_path, monkeypatch):
    queue_file = tmp_path / "manual_apply_queue.json"
    monkeypatch.setattr(manual_apply_queue, "_queue_path", lambda profile_name=None: queue_file)
    monkeypatch.setattr(manual_apply_queue.company_blacklist, "_path", lambda profile_name=None: tmp_path / str(profile_name) / "company_blacklist.json")
    monkeypatch.setattr(manual_apply_queue.config, "MANUAL_APPLY_QUEUE_FILE", str(queue_file), raising=False)
    monkeypatch.setattr(manual_apply_queue, "_queue_path", lambda profile_name=None: queue_file)

    first = manual_apply_queue.create_candidate(
        {"id": "1", "title": "QA", "company": "A"}, {"score": 70}, profile_name="qa"
    )
    second = manual_apply_queue.create_candidate(
        {"id": "2", "title": "QA", "company": "B"}, {"score": 75}, profile_name="client_42"
    )

    assert [item["token"] for item in manual_apply_queue.list_candidates("qa")] == [first["token"]]
    assert [item["token"] for item in manual_apply_queue.list_candidates("client_42")] == [second["token"]]

    manual_apply_queue.snooze_candidate(first["token"], profile_name="qa")
    assert manual_apply_queue.list_candidates("qa") == []


def test_unknown_profile_never_falls_back_to_active_queue(tmp_path, monkeypatch):
    import pytest
    import profile
    monkeypatch.setattr(manual_apply_queue.config, 'MANUAL_APPLY_QUEUE_FILE', str(tmp_path / 'active.json'))
    def missing(name):
        raise FileNotFoundError(name)
    monkeypatch.setattr(profile, 'load_profile', missing)
    with pytest.raises(FileNotFoundError):
        manual_apply_queue.get_candidate('token', profile_name='missing')
    with pytest.raises(FileNotFoundError):
        manual_apply_queue.create_candidate({}, {}, profile_name='missing')
    assert not (tmp_path / 'active.json').exists()


def test_listing_does_not_write_queue(tmp_path, monkeypatch):
    path = tmp_path / 'queue.json'
    monkeypatch.setattr(manual_apply_queue, '_queue_path', lambda profile_name=None: path)
    def forbidden(*args, **kwargs):
        raise AssertionError('read must not write')
    monkeypatch.setattr(manual_apply_queue, '_write_queue', forbidden)
    assert manual_apply_queue.list_candidates('qa') == []
    assert not path.exists()


def test_concurrent_creates_are_not_lost(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'queue.json'
    monkeypatch.setattr(manual_apply_queue, '_queue_path', lambda profile_name=None: path)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: manual_apply_queue.create_candidate({'id': str(i)}, {}, profile_name='qa'), range(30)))
    assert len(manual_apply_queue._read_queue()['items']) == 30
