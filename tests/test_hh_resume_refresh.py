import asyncio
import builtins
from types import SimpleNamespace

import pytest

from commands import resume
from hh_client import HHClient
import telegram_bot_ui as ui
from telegram_bot import TelegramBot


@pytest.fixture
def refresh(tmp_path, monkeypatch):
    path = tmp_path / "resume.md"
    path.write_text("old resume\n")
    env = tmp_path / "profile.env"
    env.write_text("HH_PRIMARY_RESUME_ID=exact-id\nHH_PRIMARY_RESUME_TITLE=Selected title\n")
    monkeypatch.setattr(resume.config, "RESUME_FILE", str(path))
    monkeypatch.setattr(resume.config, "HH_PRIMARY_RESUME_ID", "exact-id")
    monkeypatch.setattr(resume.config, "HH_PRIMARY_RESUME_TITLE", "Selected title")
    monkeypatch.setattr(builtins, "input", lambda *a: pytest.fail("refresh must never prompt"))

    class Client:
        catalog = [{"id": "wrong", "title": "Selected title"},
                   {"id": "exact-id", "title": "Updated HH title"}]
        result = {"title": "Updated HH title", "raw": "# Updated HH title\n\nNew content", "sections": {"Skills": "New content"}}
        failure = None
        cleanup_failure = None
        logged_in = True
        downloads = []
        stopped = False

        async def start(self):
            pass

        async def is_logged_in(self):
            return self.logged_in

        async def get_resume_ids(self):
            return self.catalog

        async def download_resume_by_id(self, selected, *, strict):
            self.downloads.append((selected, strict))
            if self.failure:
                raise self.failure
            return self.result

        async def stop(self, *, persist_cookies):
            assert persist_cookies is False
            assert path.read_text() == "old resume\n"
            self.__class__.stopped = True
            if self.cleanup_failure:
                raise self.cleanup_failure

    monkeypatch.setattr(resume, "HHClient", Client)
    return path, env, Client


def test_refresh_downloads_only_exact_id_and_atomically_replaces(refresh, monkeypatch, capsys):
    path, env, client = refresh
    before_env = env.read_bytes()
    replace = resume.os.replace
    commits = []

    def checked_replace(source, target):
        assert path.read_text() == "old resume\n"
        assert source != str(path)
        commits.append(target)
        replace(source, target)

    monkeypatch.setattr(resume.os, "replace", checked_replace)
    assert asyncio.run(resume.do_hh_resume_refresh()) is True
    assert client.downloads == [(client.catalog[1], True)]
    assert client.stopped and commits == [path]
    assert path.read_text() == client.result["raw"] + "\n"
    assert env.read_bytes() == before_env
    assert resume.config.HH_PRIMARY_RESUME_ID == "exact-id"
    assert resume.config.HH_PRIMARY_RESUME_TITLE == "Selected title"
    assert "Название: Updated HH title\nID: exact-id" in capsys.readouterr().out


@pytest.mark.parametrize("catalog", [[], [{"id": "wrong", "title": "Selected title"}],
                                    [{"id": "exact-id-extra", "title": "Selected title"}],
                                    [{"id": "exact-id"}, {"id": "exact-id"}]])
def test_missing_or_ambiguous_exact_id_never_falls_back(refresh, catalog, capsys):
    path, env, client = refresh
    client.catalog = catalog
    assert asyncio.run(resume.do_hh_resume_refresh()) is False
    assert not client.downloads and client.stopped
    assert path.read_text() == "old resume\n"
    assert "не найдено однозначно" in capsys.readouterr().out


@pytest.mark.parametrize("result", [None, {}, {"raw": " \n", "title": "Title"},
                                    {"raw": "body", "title": ""},
                                    {"ok": False, "raw": "body", "title": "Title"}])
def test_failed_or_empty_download_preserves_file(refresh, result):
    path, _, client = refresh
    client.result = result
    assert asyncio.run(resume.do_hh_resume_refresh()) is False
    assert path.read_text() == "old resume\n" and client.stopped


@pytest.mark.parametrize("stage", ["failure", "cleanup_failure"])
def test_download_or_shutdown_exception_preserves_file(refresh, stage):
    path, _, client = refresh
    setattr(client, stage, RuntimeError("synthetic failure"))
    assert asyncio.run(resume.do_hh_resume_refresh()) is False
    assert path.read_text() == "old resume\n" and client.stopped


def test_unproven_shutdown_preserves_file(refresh, monkeypatch):
    path, _, client = refresh
    async def stop(self, **kwargs):
        return False
    monkeypatch.setattr(client, "stop", stop)
    assert asyncio.run(resume.do_hh_resume_refresh()) is False
    assert path.read_text() == "old resume\n"


def test_missing_configured_id_does_not_open_browser(refresh, monkeypatch):
    path, _, client = refresh
    monkeypatch.setattr(resume.config, "HH_PRIMARY_RESUME_ID", "")
    assert asyncio.run(resume.do_hh_resume_refresh()) is False
    assert not client.stopped and not client.downloads and path.read_text() == "old resume\n"


def test_lost_auth_preserves_file(refresh):
    path, _, client = refresh
    client.logged_in = False
    assert asyncio.run(resume.do_hh_resume_refresh()) is False
    assert not client.downloads and client.stopped and path.read_text() == "old resume\n"


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_atomic_write_failure_preserves_old_file(refresh, monkeypatch, operation):
    path, _, client = refresh
    def fail(*args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(resume.os, operation, fail)
    assert asyncio.run(resume.do_hh_resume_refresh()) is False
    assert path.read_text() == "old resume\n"
    assert not list(path.parent.glob(".resume.md.*.tmp"))


def test_refresh_does_not_overwrite_concurrent_local_edit(tmp_path):
    path = tmp_path / "resume.md"
    path.write_text("user's newer edit")
    with pytest.raises(RuntimeError, match="изменилось"):
        resume._publish_refreshed_resume(path, "downloaded", b"old version")
    assert path.read_text() == "user's newer edit"


def test_cancelled_download_preserves_old_file(refresh):
    path, _, client = refresh
    client.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(resume.do_hh_resume_refresh())
    assert client.stopped and path.read_text() == "old resume\n"


def test_changed_selection_before_publish_preserves_old_file(refresh, monkeypatch):
    path, _, client = refresh
    stop = client.stop
    async def changed_stop(self, **kwargs):
        await stop(self, **kwargs)
        monkeypatch.setattr(resume.config, "HH_PRIMARY_RESUME_ID", "new-id")
    monkeypatch.setattr(client, "stop", changed_stop)
    assert asyncio.run(resume.do_hh_resume_refresh()) is False
    assert path.read_text() == "old resume\n"


class DownloadPage:
    def __init__(self, *, final_url=None, error=None, status=200, title=True, sections=True):
        self.url = "https://hh.ru/resume/previous"
        self.final_url, self.error, self.status = final_url, error, status
        self.title, self.sections, self.visited = title, sections, []

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        if self.error:
            raise self.error
        self.url = self.final_url or url
        return SimpleNamespace(status=self.status)

    async def wait_for_timeout(self, *args):
        pass

    async def evaluate(self, *args):
        pass

    async def query_selector(self, selector):
        if ("title-position" in selector and self.title) or ("skills-card" in selector and self.sections):
            async def text():
                return "Fresh HH title" if "title-position" in selector else "Real skills"
            return SimpleNamespace(inner_text=text)

    async def query_selector_all(self, selector):
        return []


def download(page, monkeypatch):
    monkeypatch.setattr(resume.config, "HH_BASE_URL", "https://hh.ru")
    client = HHClient()
    client._page = page
    async def no_debug(*args):
        pass
    client._save_debug_snapshot = no_debug
    return asyncio.run(client.download_resume_by_id(
        {"id": "exact-id", "title": "Stale catalog title", "url": "https://hh.ru/resume/wrong"}, strict=True))


def test_strict_download_uses_canonical_exact_id_not_catalog_url(monkeypatch):
    page = DownloadPage()
    result = download(page, monkeypatch)
    assert page.visited == ["https://hh.ru/resume/exact-id"]
    assert result["title"] == "Fresh HH title" and "Real skills" in result["raw"]


@pytest.mark.parametrize("options", [
    {"error": TimeoutError("navigation failed")},
    {"final_url": "https://hh.ru/resume/wrong"},
    {"final_url": "https://hh.ru/account/login"},
    {"final_url": "https://foreign.example/resume/exact-id"},
    {"status": 404}, {"title": False}, {"sections": False},
])
def test_strict_download_rejects_stale_redirected_or_empty_page(monkeypatch, options):
    with pytest.raises((ValueError, TimeoutError)):
        download(DownloadPage(**options), monkeypatch)


@pytest.mark.parametrize("role", [ui.ROLE_ADMIN, ui.ROLE_USER])
@pytest.mark.parametrize("text", ["🔄 Обновить резюме", "/refresh_resume", "/refresh_resume@hunter_bot"])
def test_telegram_refresh_button_and_command_route_to_selected_profile(monkeypatch, role, text):
    bot = TelegramBot(profile_name="qa")
    monkeypatch.setattr(bot, "_selected_profile", lambda principal: "selected-profile")
    monkeypatch.setattr(bot, "_active_command", lambda profile: None)
    calls = []
    async def start(*args):
        calls.append(args)
    monkeypatch.setattr(bot, "_start_cli_command", start)
    principal = {"user_id": 42, "role": role}
    command, arg = ui._resolve_message_command(text, role)
    asyncio.run(bot._dispatch(42, principal, command, arg))
    assert calls == [(42, principal, "selected-profile", "--refresh-resume", "refresh resume", 1800)]
    keyboard = ui.build_reply_markup(role, menu=ui.MENU_SEARCH)["keyboard"]
    assert [{"text": ui.BUTTON_SEARCH_RESUME}, {"text": ui.BUTTON_REFRESH_RESUME}] in keyboard
    assert ui._command_conflicts_with_active(command)


def test_refresh_is_blocked_while_current_profile_has_active_command(monkeypatch):
    bot = TelegramBot(profile_name="qa")
    monkeypatch.setattr(bot, "_selected_profile", lambda principal: "qa")
    monkeypatch.setattr(bot, "_active_command", lambda profile: {"label": "search"})
    busy = []
    async def report(*args, **kwargs):
        busy.append(kwargs)
    monkeypatch.setattr(bot, "_send_busy_status", report)
    asyncio.run(bot._dispatch(42, {"user_id": 42, "role": ui.ROLE_USER}, "/refresh_resume", ""))
    assert busy == [{"profile_name": "qa"}]


@pytest.mark.parametrize("success", [True, False])
def test_cli_refresh_routes_noninteractively_under_profile_lock(monkeypatch, success):
    import agent
    calls = []
    monkeypatch.setattr("sys.argv", ["agent.py", "--profile", "qa", "--refresh-resume"])
    monkeypatch.setattr(agent.profile_mod, "activate", lambda name: calls.append(("locked", name)))
    monkeypatch.setattr(agent.profile_mod, "activate_no_lock", lambda name: pytest.fail("refresh must lock profile"))
    monkeypatch.setattr(agent, "_configure_logging", lambda **kw: None)
    async def refresh_command():
        calls.append("refresh")
        return success
    async def close():
        pass
    monkeypatch.setattr(agent, "do_hh_resume_refresh", refresh_command)
    monkeypatch.setattr(agent, "close_office_session", close)
    monkeypatch.setattr(agent, "close_notify_session", close)
    monkeypatch.setattr(agent, "close_llm_client", close)
    if success:
        asyncio.run(agent.main())
    else:
        with pytest.raises(SystemExit) as exc:
            asyncio.run(agent.main())
        assert exc.value.code == 1
    assert calls == [("locked", "qa"), "refresh"]


def test_external_refresh_worker_is_recognized_by_conflict_detector(monkeypatch):
    import runtime_control
    monkeypatch.setattr(runtime_control, "read_process_cmdline",
                        lambda pid: f"python agent.py --profile qa --refresh-resume")
    found = runtime_control.find_agent_pids_for_profile("qa")
    assert found and all(item["flag"] == "--refresh-resume" for item in found)
    assert not runtime_control.find_agent_pids_for_profile("different-profile")
