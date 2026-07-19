import json
import asyncio

import client_hh_auth


class TestClientHHAuth:
    def test_catalog_and_exports_paths(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
        profile_dir = tmp_path / "profiles" / "client_42"
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.env").write_text("HH_SEARCH_QUERIES=QA\n", encoding="utf-8")

        assert client_hh_auth.hh_resume_catalog_path("client_42").endswith("/profiles/client_42/hh_resumes.json")
        assert client_hh_auth.hh_resume_exports_dir("client_42").endswith("/profiles/client_42/hh_resumes")

    def test_load_missing_catalog(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
        profile_dir = tmp_path / "profiles" / "client_42"
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.env").write_text("HH_SEARCH_QUERIES=QA\n", encoding="utf-8")

        assert client_hh_auth.load_hh_resume_catalog("client_42") == []

    def test_load_catalog(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
        profile_dir = tmp_path / "profiles" / "client_42"
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.env").write_text("HH_SEARCH_QUERIES=QA\n", encoding="utf-8")
        catalog_path = profile_dir / "hh_resumes.json"
        catalog_path.write_text(json.dumps([{"id": "123", "title": "QA Resume"}]), encoding="utf-8")

        items = client_hh_auth.load_hh_resume_catalog("client_42")
        assert items[0]["id"] == "123"

    def test_resolve_profile_falls_back_to_active_profile(self, monkeypatch):
        class DummyProfile:
            def __init__(self):
                self.name = "default"
                self.home_dir = "/tmp/profiles/client_42"

        active = DummyProfile()

        def fake_load_profile(name="default"):
            if name == "client_42":
                raise FileNotFoundError("missing nested profile")
            return active

        monkeypatch.setattr(client_hh_auth.profile_mod, "load_profile", fake_load_profile)
        profile = client_hh_auth._resolve_profile("client_42")
        assert profile is active
        assert profile.name == "client_42"

    def test_update_profile_resume_ids_writes_direct_profile_env(self, tmp_path, monkeypatch):
        class DummyProfile:
            def __init__(self, home_dir):
                self.name = "client_42"
                self.home_dir = str(home_dir)

        profile_dir = tmp_path / "profiles" / "client_42"
        profile_dir.mkdir(parents=True)
        env_file = profile_dir / "profile.env"
        env_file.write_text("HH_SEARCH_QUERIES=QA\n", encoding="utf-8")

        monkeypatch.setattr(client_hh_auth, "_resolve_profile", lambda profile_name: DummyProfile(profile_dir))

        written_path = client_hh_auth._update_profile_resume_ids(
            "client_42",
            [{"id": "resume-1", "title": "QA Resume"}],
        )

        content = env_file.read_text(encoding="utf-8")
        assert written_path == str(env_file)
        assert "HH_PRIMARY_RESUME_ID=resume-1" in content
        assert "HH_PRIMARY_RESUME_TITLE=QA Resume" in content

    def test_update_profile_resume_ids_sanitizes_multiline_titles(self, tmp_path, monkeypatch):
        class DummyProfile:
            def __init__(self, home_dir):
                self.name = "client_42"
                self.home_dir = str(home_dir)

        profile_dir = tmp_path / "profiles" / "client_42"
        profile_dir.mkdir(parents=True)
        env_file = profile_dir / "profile.env"
        env_file.write_text("HH_SEARCH_QUERIES=QA\n", encoding="utf-8")

        monkeypatch.setattr(client_hh_auth, "_resolve_profile", lambda profile_name: DummyProfile(profile_dir))

        client_hh_auth._update_profile_resume_ids(
            "client_42",
            [{"id": "resume-1", "title": "QA Resume\nОбновлено вчера"}],
        )

        content = env_file.read_text(encoding="utf-8")
        assert "HH_PRIMARY_RESUME_TITLE=QA Resume Обновлено вчера" in content
        assert "HH_PRIMARY_RESUME_TITLE=QA Resume\nОбновлено вчера" not in content

    def test_run_hh_auth_capture_does_not_navigate_login_page_while_waiting(self, monkeypatch):
        class DummyProfile:
            def __init__(self):
                self.name = "client_42"
                self.home_dir = "/tmp/profiles/client_42"
                self.hh = type("HH", (), {"cookies_file": "/tmp/profiles/client_42/hh_cookies.json"})()

        class FakePage:
            def __init__(self):
                self.goto_calls = []

            async def goto(self, url, wait_until=None, timeout=None):
                self.goto_calls.append(url)

            def is_closed(self):
                return False

        class FakeClient:
            instance = None

            def __init__(self):
                FakeClient.instance = self
                self._page = FakePage()
                self._passive_checks = 0

            async def start(self, headless=False):
                return None

            async def is_logged_in_passive(self):
                self._passive_checks += 1
                return self._passive_checks >= 2

            async def save_session(self):
                return None

            async def stop(self):
                return None

        imported_calls = []

        async def fake_import_current_hh_resumes(client, profile_name):
            imported_calls.append((client, profile_name))
            return {"ok": True, "count": 1, "catalog_path": "/tmp/catalog.json", "profile_env_path": "/tmp/profile.env", "resume_file": "/tmp/resume.md", "resumes": [{"id": "1", "title": "QA"}]}

        async def fake_sleep(seconds):
            return None

        monkeypatch.setattr(client_hh_auth, "_resolve_profile", lambda profile_name: DummyProfile())
        monkeypatch.setattr(client_hh_auth, "HHClient", FakeClient)
        monkeypatch.setattr(client_hh_auth, "import_current_hh_resumes", fake_import_current_hh_resumes)
        monkeypatch.setattr(client_hh_auth.asyncio, "sleep", fake_sleep)

        result = asyncio.run(
            client_hh_auth.run_hh_auth_capture("client_42", timeout_sec=5, poll_sec=1, activate_profile=False)
        )

        assert result["ok"] is True
        assert result["authenticated"] is True
        assert result["imported_resumes"] is False
        assert result["count"] == 0
        assert imported_calls == []
        assert FakeClient.instance._page.goto_calls == ["https://hh.ru/account/login"]

    def test_run_hh_auth_capture_imports_resumes_only_when_requested(self, monkeypatch):
        class DummyProfile:
            def __init__(self):
                self.name = "client_42"
                self.home_dir = "/tmp/profiles/client_42"
                self.hh = type("HH", (), {"cookies_file": "/tmp/profiles/client_42/hh_cookies.json"})()

        class FakePage:
            async def goto(self, url, wait_until=None, timeout=None):
                return None

            def is_closed(self):
                return False

        class FakeClient:
            def __init__(self):
                self._page = FakePage()

            async def start(self, headless=False):
                return None

            async def is_logged_in_passive(self):
                return True

            async def save_session(self):
                return None

            async def stop(self):
                return None

        imported_calls = []

        async def fake_import_current_hh_resumes(client, profile_name):
            imported_calls.append((client, profile_name))
            return {
                "ok": True,
                "count": 1,
                "catalog_path": "/tmp/catalog.json",
                "profile_env_path": "/tmp/profile.env",
                "resume_file": "/tmp/resume.md",
                "resumes": [{"id": "1", "title": "QA"}],
            }

        monkeypatch.setattr(client_hh_auth, "_resolve_profile", lambda profile_name: DummyProfile())
        monkeypatch.setattr(client_hh_auth, "HHClient", FakeClient)
        monkeypatch.setattr(client_hh_auth, "import_current_hh_resumes", fake_import_current_hh_resumes)

        result = asyncio.run(
            client_hh_auth.run_hh_auth_capture(
                "client_42",
                timeout_sec=5,
                poll_sec=1,
                activate_profile=False,
                import_resumes=True,
            )
        )

        assert result["ok"] is True
        assert result["authenticated"] is True
        assert result["imported_resumes"] is True
        assert result["count"] == 1
        assert imported_calls and imported_calls[0][1] == "client_42"

    def test_run_hh_auth_capture_returns_friendly_error_when_window_closed(self, monkeypatch):
        class DummyProfile:
            def __init__(self):
                self.name = "client_42"
                self.home_dir = "/tmp/profiles/client_42"
                self.hh = type("HH", (), {"cookies_file": "/tmp/profiles/client_42/hh_cookies.json"})()

        class FakePage:
            async def goto(self, url, wait_until=None, timeout=None):
                return None

            def is_closed(self):
                return True

        class FakeClient:
            def __init__(self):
                self._page = FakePage()

            async def start(self, headless=False):
                return None

            async def stop(self):
                return None

        monkeypatch.setattr(client_hh_auth, "_resolve_profile", lambda profile_name: DummyProfile())
        monkeypatch.setattr(client_hh_auth, "HHClient", FakeClient)

        result = asyncio.run(
            client_hh_auth.run_hh_auth_capture("client_42", timeout_sec=5, poll_sec=1, activate_profile=False)
        )

        assert result["ok"] is False
        assert result["authenticated"] is False
        assert "закрыто" in result["error"].lower()


def test_hh_auth_prompt_detection_and_code_normalization():
    assert client_hh_auth._normalize_hh_auth_code("12 34-56") == "123456"
    assert client_hh_auth._looks_like_hh_auth_code_prompt("Введите код из SMS") is True
    assert client_hh_auth._looks_like_hh_auth_login_prompt("Войти по телефону или email", "https://hh.ru/account/login") is True
    assert client_hh_auth._looks_like_hh_auth_login_prompt("Введите код из SMS", "https://hh.ru/account/login") is False


def test_select_profile_resumes_filters_blank_ids_and_non_qa_titles():
    resumes = [
        {"id": "qa-1", "title": "QA Engineer"},
        {"id": "", "title": "QA Engineer duplicate"},
        {"id": "electric-1", "title": "Техник-электрик"},
        {"id": "qa-2", "title": "Тестировщик"},
        {"id": "qa-3", "title": "QA Engineer / Manual QA / Тестировщик ПО"},
    ]

    selected = client_hh_auth._select_profile_resumes("qa", resumes)

    assert [item["id"] for item in selected] == ["qa-1", "qa-2", "qa-3"]
