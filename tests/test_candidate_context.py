"""Candidate data must not follow the previously active profile."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import config
import matcher
import profile
import prompt_blocks
from google_forms import answering


@pytest.fixture
def candidate_profiles(tmp_path, monkeypatch):
    # _patch_config changes many attributes; preserve all of them for other tests.
    for key, value in vars(config).copy().items():
        if key.isupper():
            monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(config, "CANDIDATE_PROFILE_ISOLATED", False, raising=False)
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(profile, "_profiles_root_home", str(tmp_path))
    monkeypatch.setattr(profile, "_active_profile", None)
    monkeypatch.setattr(profile, "_default_candidate_settings", None)
    for key in profile._CANDIDATE_CONFIG_KEYS:
        monkeypatch.setattr(config, key, "GLOBAL_ADMIN_ONLY")
    monkeypatch.setattr(config, "HH_PRIMARY_RESUME_ID", "global-admin-resume")
    monkeypatch.setattr(config, "HH_SECONDARY_RESUME_ID", "global-admin-secondary")
    monkeypatch.setenv("CANDIDATE_EMAIL", "global-admin@example.com")
    monkeypatch.setenv("CANDIDATE_PHONE", "global-admin-phone")
    monkeypatch.setenv("CANDIDATE_TELEGRAM", "@global_admin")
    monkeypatch.setenv("CANDIDATE_RESUME_URL", "https://hh.ru/resume/global-admin")
    dirs = {}
    for name in ("admin", "client", "empty"):
        directory = tmp_path / "profiles" / name
        directory.mkdir(parents=True)
        (directory / "profile.env").write_text("# isolated profile\n")
        (directory / "resume.md").write_text(f"RESUME_{name.upper()}_ONLY\n")
        dirs[name] = directory
    (dirs["admin"] / "profile.env").write_text(
        "HH_AUTO_ANSWER_PROFILE_NOTE=ADMIN_ENV_NOTE_ONLY\n"
        "HH_AUTO_ANSWER_SALARY_BASELINE=99000\n"
        "CANDIDATE_EMAIL=admin@example.com\n"
        "HH_PRIMARY_RESUME_ID=admin-resume\n"
        "HH_SECONDARY_RESUME_ID=admin-secondary\n"
    )
    (dirs["client"] / "profile.env").write_text(
        "HH_AUTO_ANSWER_SALARY_BASELINE=45000\n"
        "CANDIDATE_EMAIL=client@example.com\n"
        "HH_PRIMARY_RESUME_ID=client-resume\n"
    )
    for name in ("admin", "client"):
        knowledge = dirs[name] / "knowledge"
        knowledge.mkdir()
        (knowledge / "profile_note.md").write_text(f"NOTE_{name.upper()}_ONLY")
        (knowledge / "experience.md").write_text(f"EXPERIENCE_{name.upper()}_ONLY")
        (dirs[name] / "facts.json").write_text(json.dumps({"confirmed": {"skill": f"FACT_{name.upper()}_ONLY"}}))
    return dirs


def test_switching_profiles_resets_note_salary_contacts_and_resume_ids(candidate_profiles):
    for name, salary in (("admin", "99000"), ("client", "45000"), ("empty", ""), ("admin", "99000")):
        profile.activate_no_lock(name)
        assert config.HH_AUTO_ANSWER_SALARY_BASELINE == salary
        contacts = prompt_blocks.get_candidate_contacts()
        assert "global-admin" not in str(contacts)
        assert not contacts["phone"] and not contacts["telegram"]
        if name == "empty":
            assert not prompt_blocks.build_profile_note_block()
            assert not prompt_blocks.build_facts_block()
            assert not prompt_blocks.build_knowledge_base_block()
            assert not config.HH_PRIMARY_RESUME_ID
            assert not config.HH_SECONDARY_RESUME_ID
            assert contacts == dict.fromkeys(("email", "phone", "telegram", "resume_url"), "")
        else:
            assert contacts["email"] == f"{name}@example.com"
            assert contacts["resume_url"] == f"https://hh.ru/resume/{name}-resume"
            assert f"NOTE_{name.upper()}_ONLY" in prompt_blocks.build_profile_note_block()
        if name == "client":
            assert config.HH_AUTO_ANSWER_PROFILE_NOTE == ""
            assert config.HH_SECONDARY_RESUME_ID == ""


def test_note_file_wins_without_duplicate_kb_inclusion(candidate_profiles):
    profile.activate_no_lock("admin")
    assert "NOTE_ADMIN_ONLY" in prompt_blocks.build_profile_note_block()
    assert "ADMIN_ENV_NOTE_ONLY" not in prompt_blocks.build_profile_note_block()
    assert "NOTE_ADMIN_ONLY" not in prompt_blocks.build_knowledge_base_block()
    mandatory, sections = prompt_blocks._load_kb_filterable()
    assert "NOTE_ADMIN_ONLY" not in mandatory + str(sections)
    (candidate_profiles["admin"] / "knowledge" / "profile_note.md").unlink()
    assert "ADMIN_ENV_NOTE_ONLY" in prompt_blocks.build_profile_note_block()


def test_default_note_is_not_replaced_by_last_named_profile(candidate_profiles):
    profile.activate_no_lock("admin")
    assert profile.load_default_profile().candidate_settings["HH_AUTO_ANSWER_PROFILE_NOTE"] == "GLOBAL_ADMIN_ONLY"


def test_actual_cover_and_matcher_prompts_use_only_active_candidate(candidate_profiles, monkeypatch):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs["messages"][0]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Направляю резюме."))])

    async def filtered(*args, **kwargs):
        return prompt_blocks.build_knowledge_base_block()

    monkeypatch.setattr(matcher, "_get_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(prompt_blocks, "build_filtered_kb_block", filtered)
    for name in ("admin", "client", "admin", "empty", "client"):
        profile.activate_no_lock(name)
        asyncio.run(matcher.generate_cover_letter({"title": "Manual QA", "id": "test-only"}))
        for prompt in (calls[-1], matcher._build_matcher_truth_block()):
            assert "GLOBAL_ADMIN_ONLY" not in prompt
            if name == "empty":
                assert "_ADMIN_ONLY" not in prompt and "_CLIENT_ONLY" not in prompt
            else:
                assert f"NOTE_{name.upper()}_ONLY" in prompt
                assert f"FACT_{name.upper()}_ONLY" in prompt
                assert f"EXPERIENCE_{name.upper()}_ONLY" in prompt
                other = "CLIENT" if name == "admin" else "ADMIN"
                assert f"_{other}_ONLY" not in prompt
            assert "13 лет" not in prompt
            assert "электромонтаж" not in prompt
            assert "QA-стаж: около 1 года" not in prompt
        assert f"RESUME_{name.upper()}_ONLY" in calls[-1]


@pytest.mark.parametrize("style", list(matcher.COVER_STYLE_RULES))
def test_shared_fallback_contains_no_candidate_biography(style):
    text = matcher._fallback_cover_letter({"title": "QA"}, cover_style=style)
    for unsupported in ("бэкграунд", "Junior", "1 года", "Python", "Postman", "диагностик"):
        assert unsupported not in text


def test_forms_fallback_does_not_invent_technical_experience():
    for question in ("Расскажите про опыт", "Почему интересна вакансия"):
        text = answering._required_text_fallback(question)
        assert "технический" not in text and "QA" not in text
