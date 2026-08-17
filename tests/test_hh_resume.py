from hh.resume import (
    _looks_like_resume_boost_action,
    _looks_like_resume_boost_success,
    _looks_like_resume_boost_unavailable,
    _resume_matches_target,
)


def test_resume_boost_text_helpers_detect_action_and_state():
    assert _looks_like_resume_boost_action("Поднять резюме")
    assert _looks_like_resume_boost_action("Поднять")
    assert _looks_like_resume_boost_action("Обновить дату резюме")
    assert not _looks_like_resume_boost_action("Откликнуться")

    assert _looks_like_resume_boost_unavailable("Резюме можно будет поднять через 2 часа")
    assert _looks_like_resume_boost_success("Резюме поднято в поиске")


def test_resume_matches_target_by_id_title_or_url():
    resume = {
        "id": "abc123",
        "title": "QA Engineer",
        "url": "/resume/abc123?from=resume_list",
    }

    assert _resume_matches_target(resume, resume_id="abc123")
    assert _resume_matches_target(resume, resume_id="abc123", resume_title="")
    assert _resume_matches_target(resume, resume_title="QA")
    assert not _resume_matches_target(resume, resume_id="zzz", resume_title="Python developer")
