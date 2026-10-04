import asyncio
import json
from types import SimpleNamespace

import pytest

import config
import filters
import matcher
import profile


@pytest.mark.parametrize("source", ["hh", "superjob", "habr", "geekjob"])
def test_generic_policy_does_not_force_qa_profession(monkeypatch, source):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    monkeypatch.setattr(config, "VACANCY_RELEVANT_KEYWORDS", [])
    monkeypatch.setattr(config, "VACANCY_EXCLUDE_KEYWORDS", [])
    vacancy = {"title": "Электрик", "snippet": "Монтаж проводки", "source": source}
    assert filters.check_vacancy(vacancy) is None
    assert matcher.classify_vacancy_cluster(vacancy) == "generic"
    assert matcher.cover_style_for_cluster("generic") == "generic"
    assert "manual QA" not in matcher._build_cover_letter_style_block(vacancy)
    assert matcher._build_cover_letter_positioning_block(vacancy) == ""


def test_generic_policy_still_blocks_military_and_custom_excludes(monkeypatch):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    monkeypatch.setattr(config, "VACANCY_RELEVANT_KEYWORDS", [])
    monkeypatch.setattr(config, "VACANCY_EXCLUDE_KEYWORDS", ["Вахта"])
    assert filters.check_vacancy({"title": "Электрик военной организации"}) == "military_redflag"
    assert filters.check_vacancy({"title": "Электрик", "snippet": "Вахта"}) == "exclude_keywords"


def test_profile_custom_keywords_apply_to_superjob(monkeypatch):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    monkeypatch.setattr(config, "VACANCY_RELEVANT_KEYWORDS", ["ЭЛЕКТРИК"])
    monkeypatch.setattr(config, "VACANCY_EXCLUDE_KEYWORDS", [])
    assert filters.check_vacancy({"title": "Электрик", "source": "superjob"}) is None
    assert filters.check_vacancy({"title": "QA engineer", "source": "superjob"}) == "relevant_keywords"


def test_qa_policy_is_kept_by_default(monkeypatch):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "qa")
    monkeypatch.setattr(config, "VACANCY_RELEVANT_KEYWORDS", [])
    monkeypatch.setattr(config, "VACANCY_EXCLUDE_KEYWORDS", [])
    assert filters.check_vacancy({"title": "Электрик"}) == "relevant_keywords"
    assert filters.check_vacancy({"title": "QA engineer"}) is None


def test_profile_keyword_settings_do_not_leak_between_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "_profiles_root", lambda: str(tmp_path))
    for name, text in (("electrician", "VACANCY_FILTER_POLICY=generic\nVACANCY_RELEVANT_KEYWORDS=электрик||монтаж\n"), ("qa", "# defaults\n")):
        directory = tmp_path / "profiles" / name
        directory.mkdir(parents=True)
        (directory / "profile.env").write_text(text)
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    monkeypatch.setattr(config, "VACANCY_RELEVANT_KEYWORDS", ["inherited-wrong-keyword"])
    monkeypatch.setattr(config, "VACANCY_EXCLUDE_KEYWORDS", ["inherited-wrong-exclude"])

    electrician = profile.load_profile("electrician")
    qa = profile.load_profile("qa")

    assert electrician.filter_policy == "generic"
    assert electrician.filter_relevant_keywords == ["электрик", "монтаж"]
    assert electrician.filter_exclude_keywords == []
    assert qa.filter_policy == "qa"
    assert qa.filter_relevant_keywords == qa.filter_exclude_keywords == []


def test_generic_matcher_does_not_reject_a_senior_candidate_as_junior_qa(monkeypatch):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Электрик, 15 лет опыта")
    monkeypatch.setattr(matcher, "_build_matcher_truth_block", lambda: "Электрик, 15 лет опыта\n")
    monkeypatch.setattr(matcher.resume_versions, "record_input", lambda *args: None)
    prompts = []

    async def create(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        result = {"score": 95, "should_apply": True, "reason": "Опыт подходит", "red_flags": []}
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps(result)))])

    monkeypatch.setattr(matcher, "_get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    result = asyncio.run(matcher.evaluate_vacancy({"title": "Senior электрик", "snippet": "10 лет опыта", "salary": "200000"}))

    assert result["should_apply"] is True
    assert result["cluster"] == "generic"
    assert "senior_level_mismatch" not in result.get("guard_flags", [])
    assert "Manual QA" not in prompts[0]


def test_unknown_filter_policy_is_rejected():
    candidate = profile.Profile()
    with pytest.raises(ValueError):
        profile._apply_env_overrides(candidate, {"VACANCY_FILTER_POLICY": "generci"})
