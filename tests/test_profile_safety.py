"""Profile path validation and safe lock diagnostics."""
from types import SimpleNamespace

import pytest

import config
import profile
import setup_profile


@pytest.mark.parametrize("name", ["qa\n", "qa\r", "../other", "/tmp/other", ".", "..", "qa/other", "qa\\other", "qa bob", "qa;echo", "юзер", "", None, 123])
@pytest.mark.parametrize("operation", [profile.load_profile, profile.create_profile, profile.profile_env_path, profile.activate_no_lock, lambda name: profile.update_profile_env(name, {"TEST": "value"})])
def test_profile_apis_reject_invalid_names_before_path_access(name, operation, monkeypatch):
    def unexpected_path_access():
        pytest.fail("Invalid name reached filesystem path construction")

    monkeypatch.setattr(profile, "_profiles_root", unexpected_path_access)
    with pytest.raises(ValueError):
        operation(name)


@pytest.mark.parametrize("name", ["qa", "client_111000637", "qa-test", "ABC123", "default"])
def test_valid_profile_names(name):
    assert profile.validate_profile_name(name) == name


def test_default_is_reserved_for_profile_creation():
    with pytest.raises(ValueError, match="зарезервировано"):
        profile.validate_profile_name("default", allow_default=False)


def test_list_profiles_ignores_invalid_directory_names(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    for name in ("qa", "bad\n", "default"):
        directory = tmp_path / "profiles" / name
        directory.mkdir(parents=True)
        (directory / "profile.env").write_text("# profile\n")
    assert profile.list_profiles() == ["qa", "default"]


@pytest.mark.parametrize("owner", ["not-a-pid", "", "-1", "0", "123\n456"])
def test_lock_error_handles_invalid_pid_without_removal_advice(tmp_path, monkeypatch, owner):
    monkeypatch.setattr(profile, "_lock_fd", None)
    lock_file = tmp_path / ".lock"
    lock_file.write_text(owner)

    def blocked(*args):
        raise BlockingIOError("already locked")

    monkeypatch.setattr(profile.fcntl, "flock", blocked)
    with pytest.raises(profile.ProfileLockedError) as error:
        profile._acquire_lock(profile.Profile(name="qa", home_dir=str(tmp_path)))
    assert "PID из lock-файла: неизвестен" in str(error.value)
    assert "rm " not in str(error.value)
    assert lock_file.read_text() == owner


def test_lock_error_handles_pid_outside_os_integer_range(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "_lock_fd", None)
    (tmp_path / ".lock").write_text("9" * 50)

    def blocked(*args):
        raise BlockingIOError("already locked")

    monkeypatch.setattr(profile.fcntl, "flock", blocked)
    with pytest.raises(profile.ProfileLockedError, match="статус неизвестен"):
        profile._acquire_lock(profile.Profile(name="qa", home_dir=str(tmp_path)))


def test_wizard_validates_name_and_launches_login_without_shell(tmp_path, monkeypatch):
    answers = iter(["../unsafe", "safe_profile", "", "20"])
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(setup_profile, "_ask", lambda *args: next(answers))
    monkeypatch.setattr(setup_profile, "_ask_list", lambda *args, **kwargs: ["QA"])
    monkeypatch.setattr(setup_profile, "_ask_resume", lambda: "")
    monkeypatch.setattr(setup_profile, "_ask_source", lambda key: {"enabled": key == "hh", "has_resume": True})
    monkeypatch.setattr(setup_profile, "_ask_yn", lambda *args, **kwargs: True)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(setup_profile.subprocess, "run", run)
    setup_profile.run_wizard()
    assert calls == [(["./run.sh", "--profile", "safe_profile", "login"], {"shell": False, "check": False})]
    assert (tmp_path / "profiles" / "safe_profile" / "profile.env").is_file()
    assert not (tmp_path / "unsafe").exists()
