"""
Тесты profile.py (F-001, F-002).

Проверяем модель профиля, загрузку из env, именованные профили,
активацию профиля и патчинг config.
"""
import os
import json
import pytest
from pathlib import Path

from profile import (
    Profile,
    HHConfig,
    SuperJobConfig,
    HabrConfig,
    GeekJobConfig,
    NotifyConfig,
    SourceConfig,
    ProfileLockedError,
    load_default_profile,
    load_profile,
    list_profiles,
    create_profile,
    update_profile_env,
    activate,
    activate_no_lock,
    active,
    _acquire_lock,
    _release_lock,
    _parse_env_file,
    _apply_env_overrides,
    _active_profile,
)


class TestProfileModel:
    """Базовая модель Profile."""

    def test_default_profile_has_name(self):
        p = Profile()
        assert p.name == "default"

    def test_default_profile_state_paths(self, tmp_path):
        p = Profile(home_dir=str(tmp_path))
        assert p.seen_file == str(tmp_path / "seen_vacancies.json")
        assert p.analytics_events_file == str(tmp_path / "analytics_events.jsonl")
        assert p.analytics_state_file == str(tmp_path / "analytics_state.json")
        assert p.run_history_file == str(tmp_path / "run_history.jsonl")
        assert p.runtime_status_file == str(tmp_path / "runtime_status.json")
        assert p.daemon_pid_file == str(tmp_path / "job-hunter-daemon.pid")
        assert p.telegram_bot_pid_file == str(tmp_path / "telegram-bot.pid")
        assert p.telegram_bot_state_file == str(tmp_path / "telegram-bot-state.json")
        assert p.telegram_bot_runtime_file == str(tmp_path / "telegram-bot-runtime.json")
        assert p.log_file == str(tmp_path / "job-hunter.log")
        assert p.error_log_file == str(tmp_path / "job-hunter-errors.log")
        assert p.telegram_bot_log_file == str(tmp_path / "telegram-bot.log")
        assert p.telegram_bot_debug_log_file == str(tmp_path / "telegram-bot-debug.jsonl")
        assert p.state_dir == str(tmp_path / "state")
        assert p.resume_file == str(tmp_path / "resume.md")

    def test_custom_state_paths_not_overwritten(self, tmp_path):
        p = Profile(
            home_dir=str(tmp_path),
            seen_file="/custom/seen.json",
        )
        assert p.seen_file == "/custom/seen.json"
        # Остальные — из home_dir
        assert p.analytics_events_file == str(tmp_path / "analytics_events.jsonl")

    def test_source_configs_independent(self):
        p = Profile()
        p.hh.enabled = False
        p2 = Profile()
        # Новый Profile не должен быть затронут
        assert p2.hh.enabled is True

    def test_hh_config_defaults(self):
        hh = HHConfig()
        assert hh.enabled is True
        assert hh.auto_apply is True
        assert hh.search_queries == []
        assert hh.search_pages == 3

    def test_superjob_config_defaults(self):
        sj = SuperJobConfig()
        assert sj.enabled is True
        assert sj.resume_id == 0

    def test_habr_config_defaults(self):
        h = HabrConfig()
        assert h.min_seconds_between_applications == 10

    def test_geekjob_config_defaults(self):
        g = GeekJobConfig()
        assert g.search_pages == 5
        assert g.resume_id == ""

    def test_notify_config_defaults(self):
        n = NotifyConfig()
        assert n.chat_id == 0
        assert n.bot_token == ""


class TestLoadDefaultProfile:
    """load_default_profile() загружает из текущих env/config."""

    def test_loads_without_error(self):
        p = load_default_profile()
        assert p.name == "default"
        assert isinstance(p.hh, HHConfig)
        assert isinstance(p.superjob, SuperJobConfig)
        assert isinstance(p.habr, HabrConfig)
        assert isinstance(p.geekjob, GeekJobConfig)
        assert isinstance(p.notify, NotifyConfig)

    def test_hh_queries_populated(self):
        p = load_default_profile()
        assert len(p.hh.search_queries) > 0

    def test_state_paths_use_job_hunter_home(self):
        import config
        p = load_default_profile()
        assert p.home_dir == config.JOB_HUNTER_HOME
        assert config.JOB_HUNTER_HOME in p.seen_file


class TestParseEnvFile:
    """Парсинг .env файлов."""

    def test_basic(self, tmp_path):
        env_file = tmp_path / "test.env"
        env_file.write_text(
            "HH_ENABLED=1\n"
            "HH_SEARCH_QUERIES=QA||тестировщик\n"
            "# comment\n"
            "\n"
            "NOTIFY_CHAT_ID=12345\n"
        )
        env = _parse_env_file(str(env_file))
        assert env["HH_ENABLED"] == "1"
        assert env["HH_SEARCH_QUERIES"] == "QA||тестировщик"
        assert env["NOTIFY_CHAT_ID"] == "12345"
        assert "#" not in env

    def test_quoted_values(self, tmp_path):
        env_file = tmp_path / "test.env"
        env_file.write_text("KEY='value with spaces'\nKEY2=\"quoted\"\n")
        env = _parse_env_file(str(env_file))
        assert env["KEY"] == "value with spaces"
        assert env["KEY2"] == "quoted"


class TestApplyEnvOverrides:
    """Применение переопределений из env к профилю."""

    def test_override_hh_enabled(self):
        p = Profile()
        p.hh.enabled = True
        _apply_env_overrides(p, {"HH_ENABLED": "0"})
        assert p.hh.enabled is False

    def test_override_queries(self):
        p = Profile()
        p.hh.search_queries = ["old"]
        _apply_env_overrides(p, {"HH_SEARCH_QUERIES": "QA||DevOps"})
        assert p.hh.search_queries == ["QA", "DevOps"]

    def test_override_notify(self):
        p = Profile()
        _apply_env_overrides(p, {"NOTIFY_CHAT_ID": "999"})
        assert p.notify.chat_id == 999

    def test_override_limits(self):
        p = Profile()
        _apply_env_overrides(p, {
            "MAX_APPLICATIONS_PER_RUN": "50",
            "MAX_AUTO_APPLICATIONS_PER_SOURCE": "10",
        })
        assert p.max_applications_per_run == 50
        assert p.max_auto_applications_per_source == 10

    def test_no_override_when_key_missing(self):
        p = Profile()
        p.hh.enabled = True
        _apply_env_overrides(p, {})
        assert p.hh.enabled is True


class TestLoadProfile:
    """load_profile() — загрузка именованных профилей."""

    def test_default_profile(self):
        p = load_profile("default")
        assert p.name == "default"

    def test_missing_profile_raises(self):
        with pytest.raises(FileNotFoundError, match="не найден"):
            load_profile("nonexistent_profile_xyz")

    def test_named_profile(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        # Создаём профиль
        profile_dir = tmp_path / "profiles" / "alice"
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.env").write_text(
            "HH_ENABLED=0\n"
            "SUPERJOB_ENABLED=1\n"
            "HH_SEARCH_QUERIES=Python developer\n"
            "HH_SEARCH_PAGES=5\n"
            "NOTIFY_CHAT_ID=42\n"
        )

        p = load_profile("alice")
        assert p.name == "alice"
        assert p.home_dir == str(profile_dir)
        assert p.hh.enabled is False
        assert p.hh.search_queries == ["Python developer"]
        assert p.hh.search_pages == 5
        assert p.notify.chat_id == 42
        assert p.search_interval_min == 30
        # Cookies в профильной директории
        assert str(profile_dir) in p.hh.cookies_file
        # State файлы тоже в профильной директории
        assert str(profile_dir) in p.seen_file

    def test_named_profile_schedule_overrides(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        profile_dir = tmp_path / "profiles" / "alice"
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.env").write_text(
            "SEARCH_INTERVAL_MIN=480\n"
            "INVITE_CHECK_INTERVAL_MIN=1440\n"
        )

        p = load_profile("alice")
        assert p.search_interval_min == 480
        assert p.invite_check_interval_min == 1440

    def test_named_profile_recomputes_daemon_and_bot_runtime_paths(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        profile_dir = tmp_path / "profiles" / "alice"
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.env").write_text("HH_ENABLED=1\n")

        p = load_profile("alice")

        assert p.daemon_pid_file == str(profile_dir / "job-hunter-daemon.pid")
        assert p.telegram_bot_pid_file == str(profile_dir / "telegram-bot.pid")
        assert p.telegram_bot_state_file == str(profile_dir / "telegram-bot-state.json")
        assert p.telegram_bot_runtime_file == str(profile_dir / "telegram-bot-runtime.json")
        assert p.log_file == str(profile_dir / "job-hunter.log")
        assert p.error_log_file == str(profile_dir / "job-hunter-errors.log")
        assert p.telegram_bot_log_file == str(profile_dir / "telegram-bot.log")
        assert p.telegram_bot_debug_log_file == str(profile_dir / "telegram-bot-debug.jsonl")


class TestListProfiles:
    """list_profiles() — список доступных профилей."""

    def test_always_has_default(self):
        profiles = list_profiles()
        assert "default" in profiles

    def test_finds_named_profiles(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        for name in ["alice", "bob"]:
            d = tmp_path / "profiles" / name
            d.mkdir(parents=True)
            (d / "profile.env").write_text(f"# {name}\n")

        profiles = list_profiles()
        assert "default" in profiles
        assert "alice" in profiles
        assert "bob" in profiles
        assert profiles[-1] == "default"

    def test_ignores_dirs_without_env(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        (tmp_path / "profiles" / "broken").mkdir(parents=True)
        # Нет profile.env

        profiles = list_profiles()
        assert "broken" not in profiles

    def test_prefers_qa_before_other_named_profiles(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        for name in ["electrician", "qa"]:
            d = tmp_path / "profiles" / name
            d.mkdir(parents=True)
            (d / "profile.env").write_text(f"# {name}\n")

        profiles = list_profiles()
        assert profiles == ["qa", "electrician", "default"]


class TestActivateProfile:
    """activate() — активация профиля и патчинг config."""

    def test_activate_default(self, monkeypatch):
        import profile as profile_mod
        # Сбрасываем глобальное состояние
        monkeypatch.setattr(profile_mod, "_active_profile", None)

        p = activate("default")
        assert p.name == "default"
        assert active() is p

    def test_activate_patches_config(self, tmp_path, monkeypatch):
        import config
        import profile as profile_mod
        monkeypatch.setattr(profile_mod, "_active_profile", None)
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        # Создаём профиль
        profile_dir = tmp_path / "profiles" / "tester"
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.env").write_text(
            "HH_ENABLED=0\n"
            "NOTIFY_CHAT_ID=777\n"
            "MAX_APPLICATIONS_PER_RUN=42\n"
        )

        p = activate("tester")

        # config.* должен быть пропатчен
        assert config.HH_ENABLED is False
        assert config.NOTIFY_CHAT_ID == 777
        assert config.MAX_APPLICATIONS_PER_RUN == 42
        assert config.SEEN_VACANCIES_FILE == p.seen_file
        assert config.DAEMON_PID_FILE == p.daemon_pid_file
        assert config.TELEGRAM_BOT_PID_FILE == p.telegram_bot_pid_file
        assert config.LOG_FILE == p.log_file
        assert config.ERROR_LOG_FILE == p.error_log_file
        assert config.TELEGRAM_BOT_LOG_FILE == p.telegram_bot_log_file
        assert config.TELEGRAM_BOT_DEBUG_LOG_FILE == p.telegram_bot_debug_log_file
        assert str(profile_dir) in config.SEEN_VACANCIES_FILE

    def test_activate_no_lock(self, tmp_path, monkeypatch):
        import config
        import profile as profile_mod

        monkeypatch.setattr(profile_mod, "_active_profile", None)
        monkeypatch.setattr(profile_mod, "_lock_fd", None)
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        profile_dir = tmp_path / "profiles" / "botuser"
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.env").write_text("HH_ENABLED=1\n")

        p = activate_no_lock("botuser")

        assert p.name == "botuser"
        assert profile_mod._lock_fd is None
        assert config.JOB_HUNTER_HOME == str(profile_dir)

    def test_can_load_another_named_profile_after_activation(self, tmp_path, monkeypatch):
        import config
        import profile as profile_mod

        monkeypatch.setattr(profile_mod, "_active_profile", None)
        monkeypatch.setattr(profile_mod, "_lock_fd", None)
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        for name in ("alice", "bob"):
            profile_dir = tmp_path / "profiles" / name
            profile_dir.mkdir(parents=True)
            (profile_dir / "profile.env").write_text(f"# {name}\n")

        activated = activate_no_lock("alice")
        loaded = load_profile("bob")

        assert activated.name == "alice"
        assert loaded.name == "bob"
        assert loaded.home_dir == str(tmp_path / "profiles" / "bob")

    def test_activate_isolates_cookies(self, tmp_path, monkeypatch):
        import config
        import profile as profile_mod
        monkeypatch.setattr(profile_mod, "_active_profile", None)
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        profile_dir = tmp_path / "profiles" / "user2"
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.env").write_text("# user2\n")

        activate("user2")

        assert str(profile_dir) in config.HH_COOKIES_FILE
        assert str(profile_dir) in config.SUPERJOB_COOKIES_FILE
        assert str(profile_dir) in config.HABR_COOKIES_FILE
        assert str(profile_dir) in config.GEEKJOB_COOKIES_FILE

    def test_active_returns_default_if_not_activated(self, monkeypatch):
        import profile as profile_mod
        monkeypatch.setattr(profile_mod, "_active_profile", None)

        p = active()
        assert p.name == "default"


class TestProfileLock:
    """Lock-файл профиля — защита от параллельного запуска."""

    def test_lock_created(self, tmp_path, monkeypatch):
        import profile as profile_mod
        monkeypatch.setattr(profile_mod, "_active_profile", None)
        monkeypatch.setattr(profile_mod, "_lock_fd", None)

        p = Profile(name="locktest", home_dir=str(tmp_path))
        _acquire_lock(p)
        try:
            lock_file = tmp_path / ".lock"
            assert lock_file.exists()
            content = lock_file.read_text().strip()
            assert content == str(os.getpid())
        finally:
            _release_lock()

    def test_double_lock_same_process_ok(self, tmp_path, monkeypatch):
        """Повторный _acquire_lock в том же процессе — освобождает старый, берёт новый."""
        import profile as profile_mod
        monkeypatch.setattr(profile_mod, "_lock_fd", None)

        p = Profile(name="locktest2", home_dir=str(tmp_path))
        _acquire_lock(p)
        _acquire_lock(p)  # не должен упасть
        _release_lock()

    def test_concurrent_lock_blocked(self, tmp_path):
        """Второй процесс не может захватить lock того же профиля."""
        import subprocess
        import sys

        p = Profile(name="locktest3", home_dir=str(tmp_path))

        # Захватываем lock в текущем процессе
        import profile as profile_mod
        old_fd = profile_mod._lock_fd
        profile_mod._lock_fd = None
        _acquire_lock(p)

        try:
            # Пытаемся захватить из дочернего процесса через скрипт
            script = tmp_path / "check_lock.py"
            script.write_text(
                "import sys\n"
                "sys.path.insert(0, '.')\n"
                "from profile import Profile, _acquire_lock, ProfileLockedError\n"
                f"p = Profile(name='locktest3', home_dir='{tmp_path}')\n"
                "try:\n"
                "    _acquire_lock(p)\n"
                "    print('ACQUIRED')\n"
                "except ProfileLockedError:\n"
                "    print('BLOCKED')\n"
            )
            result = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert "BLOCKED" in result.stdout, f"stdout={result.stdout!r} stderr={result.stderr!r}"
        finally:
            _release_lock()
            profile_mod._lock_fd = old_fd

    def test_release_allows_reacquire(self, tmp_path, monkeypatch):
        import profile as profile_mod
        monkeypatch.setattr(profile_mod, "_lock_fd", None)

        p = Profile(name="locktest4", home_dir=str(tmp_path))
        _acquire_lock(p)
        _release_lock()

        # Должен захватиться снова
        _acquire_lock(p)
        _release_lock()

    def test_activate_acquires_lock(self, tmp_path, monkeypatch):
        import config
        import profile as profile_mod
        monkeypatch.setattr(profile_mod, "_active_profile", None)
        monkeypatch.setattr(profile_mod, "_lock_fd", None)
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        profile_dir = tmp_path / "profiles" / "locked"
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.env").write_text("# locked\n")

        activate("locked")

        lock_file = profile_dir / ".lock"
        assert lock_file.exists()
        assert lock_file.read_text().strip() == str(os.getpid())
        _release_lock()


class TestCreateProfile:
    """create_profile() — создание нового профиля."""

    def test_creates_directory_and_env(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        p = create_profile("alice")
        assert p.name == "alice"

        profile_dir = tmp_path / "profiles" / "alice"
        assert profile_dir.is_dir()
        assert (profile_dir / "profile.env").is_file()

    def test_env_contains_queries(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        create_profile("bob", search_queries=["Python developer", "Django"])
        env_content = (tmp_path / "profiles" / "bob" / "profile.env").read_text()
        assert "Python developer" in env_content
        assert "Django" in env_content

    def test_env_contains_explicit_schedule_defaults(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        create_profile("frank")
        env_content = (tmp_path / "profiles" / "frank" / "profile.env").read_text()
        assert "SEARCH_INTERVAL_MIN=480" in env_content
        assert "INVITE_CHECK_INTERVAL_MIN=480" in env_content

    def test_duplicate_raises(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        create_profile("carol")
        with pytest.raises(FileExistsError, match="уже существует"):
            create_profile("carol")

    def test_default_name_raises(self):
        with pytest.raises(ValueError):
            create_profile("default")

    def test_empty_name_raises(self):
        with pytest.raises(ValueError):
            create_profile("")

    def test_invalid_name_raises(self):
        with pytest.raises(ValueError, match="Недопустимое"):
            create_profile("alice bob")

    def test_created_profile_appears_in_list(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        create_profile("dave")
        profiles = list_profiles()
        assert "dave" in profiles

    def test_created_profile_is_loadable(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        create_profile("eve")
        p = load_profile("eve")
        assert p.name == "eve"
        assert p.hh.enabled is True


class TestUpdateProfileEnv:
    def test_updates_existing_and_new_keys(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        profile_dir = tmp_path / "profiles" / "alice"
        profile_dir.mkdir(parents=True)
        env_file = profile_dir / "profile.env"
        env_file.write_text("HH_ENABLED=1\n")

        result = update_profile_env("alice", {"SEARCH_INTERVAL_MIN": 480, "HH_ENABLED": 0})
        content = env_file.read_text()

        assert result == str(env_file)
        assert "HH_ENABLED=0" in content
        assert "SEARCH_INTERVAL_MIN=480" in content

    def test_sanitizes_multiline_values(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))

        profile_dir = tmp_path / "profiles" / "alice"
        profile_dir.mkdir(parents=True)
        env_file = profile_dir / "profile.env"
        env_file.write_text("HH_ENABLED=1\n")

        update_profile_env("alice", {"HH_PRIMARY_RESUME_TITLE": "QA Resume\nОбновлено вчера"})
        content = env_file.read_text()

        assert "HH_PRIMARY_RESUME_TITLE=QA Resume Обновлено вчера" in content
        assert "HH_PRIMARY_RESUME_TITLE=QA Resume\nОбновлено вчера" not in content
