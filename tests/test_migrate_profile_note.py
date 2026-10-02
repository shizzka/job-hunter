import stat

import pytest

from migrate_profile_note import migrate_profile_note


def test_migration_is_explicit_private_and_idempotent(tmp_path):
    env = tmp_path / "global.env"
    original = 'API_KEY=secret\nHH_AUTO_ANSWER_PROFILE_NOTE="Only this candidate"\n'
    env.write_text(original)
    directory = tmp_path / "qa"
    directory.mkdir()
    (directory / "profile.env").write_text("# profile\n")
    target = migrate_profile_note(str(env), str(directory))
    assert target.read_text() == "Only this candidate\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert migrate_profile_note(str(env), str(directory)) == target
    assert env.read_text() == original
    target.write_text("User edit\n")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        migrate_profile_note(str(env), str(directory))
    assert target.read_text() == "User edit\n"


def test_migration_refuses_missing_profile_or_note(tmp_path):
    env = tmp_path / "global.env"
    env.write_text("# no candidate note\n")
    directory = tmp_path / "qa"
    directory.mkdir()
    with pytest.raises(ValueError, match="existing named profile"):
        migrate_profile_note(str(env), str(directory))
    (directory / "profile.env").write_text("# profile\n")
    with pytest.raises(ValueError, match="no HH_AUTO_ANSWER_PROFILE_NOTE"):
        migrate_profile_note(str(env), str(directory))
    assert not (directory / "knowledge").exists()


def test_salary_migration_preserves_own_values_and_source(tmp_path):
    env = tmp_path / "global.env"
    original = "HH_AUTO_ANSWER_PROFILE_NOTE=Admin\nHH_AUTO_ANSWER_SALARY_BASELINE=99000\nHH_AUTO_ANSWER_SALARY_RULE=Admin rule\n"
    env.write_text(original)
    directory = tmp_path / "qa"
    directory.mkdir()
    profile_env = directory / "profile.env"
    profile_env.write_text("# Existing settings\nHH_AUTO_ANSWER_SALARY_BASELINE=80000\nOTHER=keep\n")
    migrate_profile_note(str(env), str(directory), include_salary=True)
    content = profile_env.read_text()
    assert "HH_AUTO_ANSWER_SALARY_BASELINE=80000" in content
    assert "HH_AUTO_ANSWER_SALARY_BASELINE=99000" not in content
    assert "HH_AUTO_ANSWER_SALARY_RULE=Admin rule" in content
    assert "OTHER=keep" in content
    migrate_profile_note(str(env), str(directory), include_salary=True)
    assert profile_env.read_text() == content
    assert env.read_text() == original
