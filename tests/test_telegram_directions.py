"""No network: draft, profile ownership and active search direction integration."""
import asyncio
import copy
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import pytest

import directions
import search_query_suggester
from telegram_app.directions import TelegramDirections, parse_selection


def run_async(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapped


@pytest.fixture(autouse=True)
def live_hh_session_ok(monkeypatch):
    """The wizard checks the hh session live first; tests use a healthy fake session."""
    from hh import session_probe
    monkeypatch.setattr(session_probe, "probe", lambda cookies_file, **kw: {"status": "ok", "message": "", "resumes": []})


def make_direction(home, name="qa", *, strategy="ab"):
    value = {"name": name, "title": name.upper(), "filter_policy": "qa", "search_queries": ["QA Engineer"],
             "relevant_keywords": ["qa"], "exclude_keywords": ["support"], "selection_strategy": strategy,
             "variants": [{"hh_resume_hash": f"hash{i}", "hh_resume_numeric_id": i, "title": f"Resume {i}"} for i in (1, 2)]}
    return directions.save_direction(home, value, texts={"hash1": "First resume", "hash2": "Second resume"})


class Harness(TelegramDirections):
    def __init__(self, tmp_path):
        self.selected = "client_1"
        self.profiles = {name: SimpleNamespace(home_dir=str(tmp_path / name), name=name,
                        hh=SimpleNamespace(cookies_file=str(tmp_path / name / "hh_cookies.json")))
                         for name in ("client_1", "client_2")}
        self.sent = []
        self.auth_calls = []
        self.ai_allowed = True
        self.command = None
        self.downloads = []

    def _selected_profile(self, principal):
        return principal.get("profile") if principal.get("role") != "admin" else self.selected

    def _profile(self, name):
        return self.profiles[name]

    def _set_selected_menu(self, user_id, menu):
        pass

    async def _send_text(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    def _menu_reply_markup(self, principal, menu=None):
        return {"keyboard": [[{"text": str(menu)}]]}

    def _reload_profile_daemon(self, name):
        return False

    def _has_active_command(self, name):
        return self.command is not None

    async def _send_busy_status(self, chat_id, principal, *, profile_name):
        await self._send_text(chat_id, "Профиль занят: " + profile_name)

    def _mark_active_command(self, **kwargs):
        self.command = SimpleNamespace(task=None)
        return self.command

    def _create_active_task(self, command, runner):
        return asyncio.create_task(runner())

    def _clear_active_command(self, name):
        self.command = None

    async def _authorize_ai_usage(self, chat_id, principal, **kwargs):
        self.auth_calls.append(kwargs)
        return self.ai_allowed

    async def _direction_download(self, profile, items, *, chat_id):
        self.downloads.append((profile.name, items))
        return [{"hh_resume_hash": str(item.get("id") or item["hh_resume_hash"]),
                 "hh_resume_numeric_id": None, "title": item.get("title", "Resume"), "raw": "Downloaded resume"} for item in items]

    async def _direction_load_catalog(self, profile, *, chat_id):
        import client_hh_auth
        return client_hh_auth.load_hh_resume_catalog(profile.name)


USER = {"user_id": 1, "role": "user", "profile": "client_1"}
ADMIN = {"user_id": 99, "role": "admin"}


@run_async
async def test_work_conditions_edit_is_a_confirmed_direction_draft(bot):
    home = Path(bot.profiles["client_1"].home_dir)
    original = make_direction(home)
    directions.set_active_direction(home, original["name"])
    await bot._dispatch_direction(1, USER, "/direction_edit", "work_formats")
    state = bot._direction_sessions()[1]
    assert await bot._accept_direction_input(1, USER, "удалённо, гибрид")
    assert directions.active_direction(home) == original
    assert state["draft"]["work_conditions"]["allowed_formats"] == ["remote", "hybrid"]
    assert "Вахта — нет" in bot.sent[-1][1]
    await bot._dispatch_direction(1, USER, "/direction_edit", "fly_allowed")
    await bot._direction_callback(1, USER, f"dir:{state['token']}:conditionpreset:yes")
    await bot._dispatch_direction(1, USER, "/direction_edit", "work_cities")
    await bot._accept_direction_input(1, USER, "Город Пример")
    await bot._dispatch_direction(1, USER, "/direction_save", "")
    saved = directions.active_direction(home)
    assert saved["work_conditions"] == {"allowed_formats": ["remote", "hybrid"], "allow_fly_in_fly_out": True, "allowed_cities": ["Город Пример"]}
    assert saved["search_queries"] == original["search_queries"]
    assert saved["variants"] == original["variants"]


@pytest.fixture
def bot(tmp_path):
    return Harness(tmp_path)


@run_async
async def test_multi_select_creation_has_no_writes_until_save_and_cancel_drops_texts(bot, monkeypatch):
    monkeypatch.setattr("client_hh_auth.load_hh_resume_catalog", lambda name: [{"id": "one", "title": "One"}, {"id": "two", "title": "Two"}])
    async def suggest(**kwargs):
        return {"title": "QA", "filter_policy": "qa", "search_queries": ["QA Engineer"], "relevant_keywords": ["qa"], "exclude_keywords": []}
    monkeypatch.setattr(search_query_suggester, "suggest_direction", suggest)
    home = Path(bot.profiles["client_1"].home_dir)
    await bot._dispatch_direction(1, USER, "/direction_new", "")
    await bot.command.task
    assert await bot._accept_direction_input(1, USER, "1, 2 1")
    await bot.command.task
    assert not home.exists()
    state = bot._direction_sessions()[1]
    assert [v["hh_resume_hash"] for v in state["draft"]["variants"]] == ["one", "two"]
    assert [c.get("check_only", False) for c in bot.auth_calls] == [True, False]
    await bot._dispatch_direction(1, USER, "/direction_cancel", "")
    assert 1 not in bot._direction_sessions() and not home.exists()
    await bot._dispatch_direction(1, USER, "/direction_save", "")
    assert not home.exists()


@run_async
async def test_save_new_direction_persists_only_own_profile_and_preserves_unknown_numeric_id(bot):
    state = bot._direction_bind(USER, mode="draft", draft={"name": "new", "title": "New", "filter_policy": "generic",
        "search_queries": ["Electric engineer"], "relevant_keywords": [], "exclude_keywords": [],
        "variants": [{"hh_resume_hash": "one", "title": "One", "hh_resume_numeric_id": None}]}, texts={"one": "Text"})
    assert not Path(state["home_dir"]).exists()
    await bot._dispatch_direction(1, USER, "/direction_save", "")
    saved = directions.active_direction(state["home_dir"])
    assert saved["name"] == "new" and saved["variants"][0]["hh_resume_numeric_id"] is None
    assert Path(saved["variants"][0]["text_file"]).read_text() == "Text"
    assert not Path(bot.profiles["client_2"].home_dir).exists()


@run_async
async def test_activation_and_variant_selection_require_confirmation(bot):
    home = bot.profiles["client_1"].home_dir
    make_direction(home)
    make_direction(home, "other")
    directions.set_active_direction(home, "qa")
    await bot._dispatch_direction(1, USER, "/direction_switch", "")
    await bot._accept_direction_input(1, USER, "1")  # Alphabetical 'other'
    assert directions.active_direction(home)["name"] == "qa"
    await bot._dispatch_direction(1, USER, "/direction_save", "")
    assert directions.active_direction(home)["name"] == "other"
    await bot._dispatch_direction(1, USER, "/search_resume", "")
    await bot._accept_direction_input(1, USER, "2")
    assert directions.active_direction(home)["selection_strategy"] == "ab"
    await bot._dispatch_direction(1, USER, "/direction_save", "")
    selected = directions.active_direction(home)
    assert selected["selection_strategy"] == "single"
    variant = directions.select_variant(selected, "vacancy")
    assert variant["hh_resume_hash"] == "hash2"
    assert Path(variant["text_file"]).read_text() == "Second resume"


@run_async
@pytest.mark.parametrize("operation", ["pause", "remove", "resume"])
async def test_variant_updates_and_cancel_do_not_mutate_direction(bot, operation):
    home = bot.profiles["client_1"].home_dir
    original = make_direction(home)
    directions.set_active_direction(home, "qa")
    await bot._dispatch_direction(1, USER, "/direction_" + operation, "")
    await bot._accept_direction_input(1, USER, "2")
    assert directions.active_direction(home) == original
    await bot._dispatch_direction(1, USER, "/direction_cancel", "")
    assert directions.active_direction(home) == original


@run_async
async def test_stale_admin_profile_and_direction_change_reject_save(bot):
    home = bot.profiles["client_1"].home_dir
    original = make_direction(home)
    directions.set_active_direction(home, "qa")
    await bot._dispatch_direction(99, ADMIN, "/direction_single", "")
    bot.selected = "client_2"
    await bot._dispatch_direction(99, ADMIN, "/direction_save", "")
    assert directions.active_direction(home) == original
    assert not Path(bot.profiles["client_2"].home_dir).exists()
    bot.selected = "client_1"
    await bot._dispatch_direction(99, ADMIN, "/direction_single", "")
    changed = copy.deepcopy(original)
    changed["title"] = "Changed independently"
    directions.save_direction(home, changed)
    await bot._dispatch_direction(99, ADMIN, "/direction_save", "")
    assert directions.active_direction(home)["selection_strategy"] == "ab"
    assert "Устаревшие изменения" in bot.sent[-1][1]


@run_async
async def test_active_query_edit_saves_direction_only_after_confirmation(bot):
    home = bot.profiles["client_1"].home_dir
    original = make_direction(home)
    directions.set_active_direction(home, "qa")
    await bot._dispatch_direction(1, USER, "/search_edit", "")
    await bot._accept_direction_input(1, USER, "Manual QA\nAPI QA")
    assert directions.active_direction(home) == original
    await bot._dispatch_direction(1, USER, "/search_use_draft", "")
    assert directions.active_direction(home)["search_queries"] == ["Manual QA", "API QA"]
    assert not (Path(home) / "profile.env").exists()


@run_async
async def test_active_suggest_uses_all_active_variant_texts_and_authorizes(bot, monkeypatch):
    home = bot.profiles["client_1"].home_dir
    original = make_direction(home)
    directions.set_active_direction(home, "qa")
    received = []
    async def suggest(**kwargs):
        received.append(kwargs)
        return {"title": "Suggested", "filter_policy": "qa", "search_queries": ["Manual QA"], "relevant_keywords": [], "exclude_keywords": []}
    monkeypatch.setattr(search_query_suggester, "suggest_direction", suggest)
    await bot._dispatch_direction(1, USER, "/search_suggest", "")
    await bot.command.task
    assert [r["raw"] for r in received[0]["resumes"]] == ["First resume", "Second resume"]
    assert len(bot.auth_calls) == 2
    assert directions.active_direction(home) == original
    await bot._dispatch_direction(1, USER, "/search_use_draft", "")
    assert directions.active_direction(home)["search_queries"] == ["Manual QA"]


@run_async
async def test_ai_denial_prevents_download_and_analysis(bot, monkeypatch):
    home = bot.profiles["client_1"].home_dir
    make_direction(home)
    directions.set_active_direction(home, "qa")
    bot.ai_allowed = False
    async def fail(**kwargs):
        pytest.fail("AI was invoked without authorization")
    monkeypatch.setattr(search_query_suggester, "suggest_direction", fail)
    await bot._dispatch_direction(1, USER, "/search_suggest", "")
    assert not bot.downloads and bot.command is None
    assert len(bot.auth_calls) == 1


@run_async
async def test_refresh_texts_and_application_mode_are_drafts(bot):
    home = bot.profiles["client_1"].home_dir
    original = make_direction(home)
    directions.set_active_direction(home, "qa")
    await bot._dispatch_direction(1, USER, "/direction_refresh", "")
    await bot.command.task
    assert directions.active_direction(home) == original
    assert all(Path(v["text_file"]).read_text() != "Downloaded resume" for v in original["variants"])
    await bot._dispatch_direction(1, USER, "/direction_save", "")
    updated = directions.active_direction(home)
    assert all(Path(v["text_file"]).read_text() == "Downloaded resume" for v in updated["variants"])
    # The application mode is one explicit choice: saved immediately, short confirmation (owner feedback 17–18.10).
    await bot._dispatch_direction(1, USER, "/search_application_preview", "")
    assert directions.active_direction(home)["application_mode"] == "preview"
    assert "бот только показывает вакансии без откликов" in bot.sent[-1][1]


def test_selection_rejects_zero_negative_out_of_bounds_and_accepts_unique_multi():
    assert parse_selection("1, 3 1", 3) == [0, 2]
    for text in ("0", "-1", "4", "a", ""):
        with pytest.raises(ValueError):
            parse_selection(text, 3)


@run_async
async def test_analyzer_validates_generic_direction_and_prompt_data(monkeypatch):
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"title":"Электромонтаж", "filter_policy":"generic", "search_queries":["Электрик", "Электромонтажник"], "relevant_keywords":["электрик"], "exclude_keywords":["qa"]}'))])
    monkeypatch.setattr(search_query_suggester, "get_llm_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    value = await search_query_suggester.suggest_direction(resumes=[{"title": "Electrician", "raw": "Known skills"}])
    assert value["filter_policy"] == "generic"
    assert "Known skills" in calls[0]["messages"][1]["content"]
    assert "игнорируй" in calls[0]["messages"][0]["content"]
    for broken in ({}, {**value, "filter_policy": "closest"}, {**value, "search_queries": []}):
        with pytest.raises(search_query_suggester.SearchQueryValidationError):
            search_query_suggester.normalize_direction_suggestion(broken)


@run_async
async def test_async_completion_rejects_direction_switch_and_cancel(bot, monkeypatch):
    home = bot.profiles["client_1"].home_dir
    make_direction(home)
    make_direction(home, "other")
    directions.set_active_direction(home, "qa")
    started, release = asyncio.Event(), asyncio.Event()
    async def suggest(**kwargs):
        started.set()
        await release.wait()
        return {"title": "Stale", "filter_policy": "qa", "search_queries": ["Stale QA"], "relevant_keywords": [], "exclude_keywords": []}
    monkeypatch.setattr(search_query_suggester, "suggest_direction", suggest)
    await bot._dispatch_direction(1, USER, "/search_suggest", "")
    task = bot.command.task
    await started.wait()
    directions.set_active_direction(home, "other")
    release.set()
    await task
    assert directions.active_direction(home)["name"] == "other"
    assert all(item["title"] != "Stale" for item in directions.list_directions(home))
    assert "результат отменён" in bot.sent[-1][1]


@run_async
async def test_readonly_download_captures_profile_cookie_paths_and_never_persists_cookies(bot, monkeypatch):
    import hh_client
    profile = bot.profiles["client_1"]
    instances = []
    class Client:
        def __init__(self):
            self.calls = []
            instances.append(self)
        async def start(self):
            self.calls.append("start")
        async def get_resume_ids_readonly(self):
            self.calls.append("catalog_readonly")
            return [{"id": "hash1", "title": "One", "hh_resume_numeric_id": 123}]
        async def download_resume_readonly(self, item):
            self.calls.append("download_readonly")
            return {"raw": "HH text", "hh_resume_hash": "hash1", "hh_resume_numeric_id": None}
        async def stop(self, *, persist_cookies):
            self.calls.append(("stop", persist_cookies))
    monkeypatch.setattr(hh_client, "HHClient", Client)
    exports = await TelegramDirections._direction_download(bot, profile, [{"id": "hash1"}], chat_id=1)
    assert exports[0]["hh_resume_numeric_id"] == 123
    assert instances[0]._cookie_paths.cookies_file == profile.hh.cookies_file
    assert instances[0]._ui_home == profile.home_dir
    assert instances[0].calls == ["start", "catalog_readonly", "download_readonly", ("stop", False)]
    assert not Path(profile.home_dir).exists()


@run_async
async def test_fresh_direction_catalog_includes_other_professions_without_legacy_title_filter(bot, monkeypatch):
    import hh_client
    profile = bot.profiles["client_1"]
    calls = []
    class Client:
        async def start(self):
            calls.append("start")
        async def get_resume_ids_readonly(self):
            calls.append("catalog_readonly")
            return [{"id": "qa", "title": "QA"}, {"id": "electrical", "title": "Электрик"}]
        async def stop(self, *, persist_cookies):
            calls.append(("stop", persist_cookies))
    monkeypatch.setattr(hh_client, "HHClient", Client)
    monkeypatch.setattr(bot, "_direction_load_catalog", TelegramDirections._direction_load_catalog.__get__(bot))
    await bot._dispatch_direction(1, USER, "/direction_new", "")
    await bot.command.task
    assert [v["id"] for v in bot._direction_sessions()[1]["catalog"]] == ["qa", "electrical"]
    assert "Электрик" in str(bot.sent[-1][2])
    assert calls == ["start", "catalog_readonly", ("stop", False)]
    assert not Path(profile.home_dir).exists()


@run_async
async def test_real_dispatch_routes_active_search_commands_before_legacy_env_updates(bot, monkeypatch):
    from telegram_bot import TelegramBot
    home = bot.profiles["client_1"].home_dir
    make_direction(home)
    directions.set_active_direction(home, "qa")
    bot._active_command = lambda name: None
    bot._queue_launching = {}
    bot._queue_length = lambda name: 0
    bot._selected_menu = lambda principal: "main"
    bot._menu_reply_markup = lambda principal, menu=None: {"keyboard": [[{"text": str(menu)}]]}
    observed = []
    async def dispatch(chat_id, principal, command, arg):
        observed.append(command)
    monkeypatch.setattr(bot, "_dispatch_direction", dispatch)
    for command in ("/search_resume", "/search_suggest", "/search_edit", "/search_use_draft", "/search_application_confirm",
                    "/refresh_resume", "/grabresume", "/search_settings"):
        await TelegramBot._dispatch.__wrapped__(bot, 1, USER, command, "")
    assert observed == ["/search_resume", "/search_suggest", "/search_edit", "/search_use_draft", "/search_application_confirm",
                        "/direction_refresh", "/direction_refresh", "/direction_summary"]
    assert not (Path(home) / "profile.env").exists()


@run_async
async def test_registered_client_real_bot_dispatch_uses_own_client_profile_and_rejects_admin_commands(tmp_path, monkeypatch):
    import telegram_access
    import telegram_clients
    import telegram_bot
    from runtime_context import TelegramRuntimePaths
    from telegram_bot import TelegramBot

    paths = TelegramRuntimePaths(*(str(tmp_path / "bot" / name) for name in ("pid", "state.json", "runtime.json", "bot.log", "debug.jsonl")))
    actual_bot = TelegramBot(profile_name="qa", runtime_paths=paths)
    profiles = {name: SimpleNamespace(name=name, home_dir=str(tmp_path / name), resume_file=str(tmp_path / name / "resume.md"))
                for name in ("qa", "client42", "foreign")}
    for name, profile in profiles.items():
        make_direction(profile.home_dir, name=name)
        directions.set_active_direction(profile.home_dir, name)
    monkeypatch.setattr(actual_bot, "_profile_names", lambda: list(profiles))
    monkeypatch.setattr(telegram_bot.profile_mod, "load_profile", lambda name: profiles[name])
    telegram_access.upsert_user(42, profile="qa", role="user")
    telegram_clients.upsert_client(42, profile_name="client42")
    actual_bot._set_selected_profile(42, "foreign")
    principal = telegram_access.resolve_user(42)
    assert principal["profile"] == "qa"
    assert actual_bot._selected_profile(principal) == "client42"
    sent = []
    async def send(chat_id, text, **kwargs):
        sent.append(text)
    monkeypatch.setattr(actual_bot, "_send_text", send)
    monkeypatch.setattr(actual_bot, "_reload_profile_daemon", lambda name: False)
    before_foreign = {str(p): p.read_bytes() for name in ("qa", "foreign") for p in Path(profiles[name].home_dir).rglob("*") if p.is_file()}

    # Real public dispatcher wrapper selects the registered client, ignoring an arbitrary profile argument.
    await actual_bot._dispatch(42, principal, "/directions", "foreign")
    assert "Ваши направления · профиль client42" in sent[-1]
    assert "FOREIGN" not in sent[-1] and "QA" not in sent[-1]
    for command in ("/profile", "/direction_csv"):
        await actual_bot._dispatch(42, principal, command, "foreign")
        assert "Нужны права администратора" in sent[-1]
        assert actual_bot._selected_profile(principal) == "client42"
    await actual_bot._dispatch(42, principal, "/direction_single", "foreign")
    assert directions.active_direction(profiles["client42"].home_dir)["selection_strategy"] == "ab"
    await actual_bot._dispatch(42, principal, "/direction_save", "foreign")
    assert directions.active_direction(profiles["client42"].home_dir)["selection_strategy"] == "single"
    after_foreign = {str(p): p.read_bytes() for name in ("qa", "foreign") for p in Path(profiles[name].home_dir).rglob("*") if p.is_file()}
    assert before_foreign == after_foreign


@run_async
async def test_ready_draft_save_waits_for_active_profile_worker(bot):
    home = bot.profiles["client_1"].home_dir
    original = make_direction(home)
    directions.set_active_direction(home, "qa")
    await bot._dispatch_direction(1, USER, "/direction_single", "")
    bot.command = SimpleNamespace(task=None)
    for command in ("/direction_save", "/search_use_draft"):
        await bot._dispatch_direction(1, USER, command, "")
        assert directions.active_direction(home) == original
        assert bot._direction_sessions()[1]["mode"] == "draft"
        assert "Профиль занят" in bot.sent[-1][1]
    bot.command = None
    await bot._dispatch_direction(1, USER, "/direction_save", "")
    assert directions.active_direction(home)["selection_strategy"] == "single"


@run_async
@pytest.mark.parametrize("replacement", ["cancel", "new_session"])
async def test_abandoned_download_never_consumes_ai_or_runs_analysis(bot, monkeypatch, replacement):
    started, release = asyncio.Event(), asyncio.Event()
    async def download(profile, items, *, chat_id):
        started.set()
        await release.wait()
        return [{"hh_resume_hash": "one", "title": "One", "raw": "Downloaded"}]
    async def forbidden(**kwargs):
        pytest.fail("An abandoned draft must not invoke AI")
    monkeypatch.setattr(bot, "_direction_download", download)
    monkeypatch.setattr(search_query_suggester, "suggest_direction", forbidden)
    state = bot._direction_bind(USER, mode="working", draft=None)
    await bot._direction_job(1, USER, state, "new", [{"id": "one"}])
    task = bot.command.task
    await started.wait()
    if replacement == "cancel":
        await bot._dispatch_direction(1, USER, "/direction_cancel", "")
    else:
        bot._direction_bind(USER, mode="catalog")
    release.set()
    await task
    assert [call.get("check_only", False) for call in bot.auth_calls] == [True]
    assert not Path(bot.profiles["client_1"].home_dir).exists()


@run_async
async def test_admin_snapshot_summary_and_csv_use_selected_profile_without_ai(bot, monkeypatch):
    import csv
    import io
    import json
    home = Path(bot.profiles["client_1"].home_dir)
    make_direction(home)
    (home / "negotiation_snapshots.jsonl").write_text(json.dumps({
        "id": 100, "resumeId": 2, "initialTopicType": "RESPONSE_BY_APPLICANT",
        "lastState": "INVITATION", "viewedByOpponent": True,
    }) + "\n")
    documents = []
    async def document(chat_id, **kwargs):
        documents.append(kwargs)
    monkeypatch.setattr(bot, "_send_document", document, raising=False)
    await bot._dispatch_direction(99, ADMIN, "/direction_stats", "client_2")
    assert "1 отклик · просмотрели 1 · отказов 0 · приглашений 1 (100,0%) · ждут 0" in bot.sent[-1][1]
    await bot._dispatch_direction(99, ADMIN, "/direction_csv", "client_2")
    rows = list(csv.DictReader(io.StringIO(documents[0]["content"].decode("utf-8-sig"))))
    assert documents[0]["filename"] == "client_1_directions.csv"
    assert next(r for r in rows if r["hh_resume_hash"] == "hash2")["next_stage"] == "1"
    assert not bot.auth_calls
    await bot._dispatch_direction(1, USER, "/direction_csv", "")
    assert len(documents) == 1 and "Нужны права администратора" in bot.sent[-1][1]


def inline_actions(bot):
    return [button['callback_data'] for row in bot.sent[-1][2]['reply_markup']['inline_keyboard'] for button in row]


@run_async
async def test_inline_creation_toggles_next_summary_edit_and_cancel_without_writes(bot, monkeypatch):
    monkeypatch.setattr('client_hh_auth.load_hh_resume_catalog', lambda name: [{'id': 'one', 'title': 'One'}, {'id': 'two', 'title': 'Two'}])
    async def suggest(**kwargs):
        assert kwargs['profile_dir'] == bot.profiles['client_1'].home_dir
        return {'title': 'QA', 'positioning': 'Manual QA', 'goal': ['QA Engineer'], 'filter_policy': 'qa', 'search_queries': ['QA'], 'relevant_keywords': [], 'exclude_keywords': []}
    monkeypatch.setattr(search_query_suggester, 'suggest_direction', suggest)
    await bot._dispatch_direction(1, USER, '/direction_new', '')
    await bot.command.task
    actions = inline_actions(bot)
    await bot._direction_callback(1, USER, actions[0])
    await bot._direction_callback(1, USER, actions[1])
    await bot._direction_callback(1, USER, actions[0])  # toggle off
    await bot._direction_callback(1, USER, next(a for a in actions if a.endswith(':next')))
    await bot.command.task
    state = bot._direction_sessions()[1]
    assert [v['hh_resume_hash'] for v in state['draft']['variants']] == ['two']
    assert 'Manual QA' in bot.sent[-1][1] and 'Должности: QA Engineer' in bot.sent[-1][1]
    await bot._direction_callback(1, USER, f"dir:{state['token']}:edit:positioning")
    await bot._accept_direction_input(1, USER, 'QA lead')
    assert state['draft']['positioning'] == 'QA lead'
    await bot._direction_callback(1, USER, f"dir:{state['token']}:cancel")
    assert not Path(bot.profiles['client_1'].home_dir).exists()


@run_async
async def test_inline_switch_is_confirmed_and_stale_or_foreign_buttons_rejected(bot):
    home = bot.profiles['client_1'].home_dir
    make_direction(home)
    make_direction(home, 'other')
    directions.set_active_direction(home, 'qa')
    await bot._dispatch_direction(1, USER, '/directions', '')
    actions = inline_actions(bot)
    assert '✓ QA (сейчас)' in str(bot.sent[-1][2])
    switch = next(a for a in actions if a.endswith('choose:0'))
    await bot._direction_callback(2, {'user_id': 2, 'role': 'user', 'profile': 'client_2'}, switch)
    assert 'устарела' in bot.sent[-1][1]
    await bot._direction_callback(1, USER, switch)
    assert directions.active_direction(home)['name'] == 'qa'
    assert 'Искать по направлению' in bot.sent[-1][1]
    save = next(a for a in inline_actions(bot) if a.endswith(':save'))
    await bot._direction_callback(1, USER, save)
    assert directions.active_direction(home)['name'] == 'other'
    await bot._direction_callback(1, USER, save)
    assert 'устарела' in bot.sent[-1][1]


@run_async
async def test_independent_city_format_rows_and_salary_are_drafts(bot):
    home = bot.profiles['client_1'].home_dir
    original = make_direction(home)
    directions.set_active_direction(home, 'qa')
    await bot._dispatch_direction(1, USER, '/direction_summary', '')
    state = bot._direction_sessions()[1]
    token = state['token']
    await bot._direction_callback(1, USER, f'dir:{token}:edit:search_profiles')
    await bot._accept_direction_input(1, USER, '2\n113 flyInFlyOut')
    assert state['draft']['search_profiles'] == [{'area': '2', 'schedule': ''}, {'area': '113', 'schedule': 'flyInFlyOut'}]
    await bot._direction_callback(1, USER, f'dir:{token}:edit:salary')
    await bot._accept_direction_input(1, USER, '150000')
    assert state['draft']['salary_baseline'] == 150000
    assert directions.active_direction(home) == original
    await bot._direction_callback(1, USER, f'dir:{token}:cancel')
    assert directions.active_direction(home) == original


@run_async
async def test_client_stats_use_own_profile_without_ai(bot):
    home = bot.profiles['client_1'].home_dir
    make_direction(home)
    await bot._dispatch_direction(1, USER, '/direction_stats', 'client_2')
    assert 'данных пока нет' in bot.sent[-1][1]  # own (empty) profile data, argument ignored
    assert 'client_' not in bot.sent[-1][1]  # internal profile names are not shown to clients
    assert 'Нужны права' not in bot.sent[-1][1]
    assert not bot.auth_calls


@run_async
async def test_direction_router_requires_registered_private_chat(bot, monkeypatch):
    from telegram_app.callbacks import TelegramCallbackRouter
    import telegram_access
    home = bot.profiles['client_1'].home_dir
    make_direction(home)
    directions.set_active_direction(home, 'qa')
    await bot._dispatch_direction(1, USER, '/directions', '')
    payload = next(a for a in inline_actions(bot) if ':choose:' in a)
    answered = []
    async def answer(*args, **kwargs):
        answered.append((args, kwargs))
    bot._answer_callback_query = answer
    monkeypatch.setattr(telegram_access, 'resolve_user', lambda user_id: USER if user_id == 1 else None)
    callback = {'id': 'test', 'from': {'id': 1}, 'data': payload,
                'message': {'chat': {'id': 1, 'type': 'group'}}}
    await TelegramCallbackRouter._handle_callback_query.__wrapped__(bot, callback)
    assert bot._direction_sessions()[1]['mode'] == 'switch'
    assert 'личный чат' in answered[-1][0][1]
    callback['message']['chat']['type'] = 'private'
    await TelegramCallbackRouter._handle_callback_query.__wrapped__(bot, callback)
    assert bot._direction_sessions()[1]['summary'] is True
    callback['from']['id'] = 500
    await TelegramCallbackRouter._handle_callback_query.__wrapped__(bot, callback)
    assert 'Доступ закрыт' in answered[-1][0][1]


@run_async
async def test_resume_menu_keeps_unsaved_draft_and_downloaded_texts(bot):
    draft = {'name': 'new', 'title': 'New', 'filter_policy': 'generic', 'search_queries': ['Engineer'],
             'relevant_keywords': [], 'exclude_keywords': [], 'variants': [
                 {'hh_resume_hash': 'one', 'title': 'One'}, {'hh_resume_hash': 'two', 'title': 'Two'}]}
    state = bot._direction_bind(USER, mode='draft', draft=copy.deepcopy(draft), texts={'one': 'One', 'two': 'Two'})
    await bot._direction_callback(1, USER, f"dir:{state['token']}:variants")
    variant = next(a for a in inline_actions(bot) if a.endswith(':variant:1'))
    await bot._direction_callback(1, USER, variant)
    assert state['draft']['variants'][0]['hh_resume_hash'] == 'two'
    assert state['texts'] == {'one': 'One', 'two': 'Two'}
    assert not Path(state['home_dir']).exists()


@run_async
async def test_strategy_chooser_saves_existing_direction_in_one_tap(bot):
    home = bot.profiles['client_1'].home_dir
    make_direction(home, strategy='single')
    directions.set_active_direction(home, 'qa')
    await bot._dispatch_direction(1, USER, '/directions', '')
    await bot._direction_callback(1, USER, next(a for a in inline_actions(bot) if a.endswith('choose:0')))
    await bot._direction_callback(1, USER, next(a for a in inline_actions(bot) if a.endswith(':strategy')))
    assert 'Каким резюме откликаться' in bot.sent[-1][1]
    assert '✓ Всегда первое' in str(bot.sent[-1][2])
    await bot._direction_callback(1, USER, next(a for a in inline_actions(bot) if a.endswith(':strategyset:closest')))
    assert directions.active_direction(home)['selection_strategy'] == 'closest'
    assert 'Направление сохранено' in bot.sent[-1][1]



@run_async
async def test_empty_stats_offer_refresh_and_refresh_collects_both_filters(bot, monkeypatch):
    from hh import negotiation_snapshot
    home = bot.profiles["client_1"].home_dir
    make_direction(home)
    directions.set_active_direction(home, "qa")
    await bot._dispatch_direction(1, USER, "/direction_stats", "")
    assert "данных пока нет" in bot.sent[-1][1]
    assert any(a.endswith(":statsrefresh") for a in inline_actions(bot))
    calls = []
    monkeypatch.setattr(negotiation_snapshot, "collect", lambda **kw: calls.append(kw["archived"]) or {"complete": True})
    await bot._dispatch_direction(1, USER, "/direction_stats_refresh", "")
    await bot.command.task
    assert calls == [False, True] and "Данные обновлены" in " ".join(m[1] for m in bot.sent)


@run_async
async def test_select_direction_prompt_offers_direction_buttons(bot):
    """Owner 19.10: «надо переключить направление» must come with a button, not a menu hunt."""
    home = bot.profiles["client_1"].home_dir
    make_direction(home, "qa")
    make_direction(home, "electric")
    await bot._dispatch_direction(1, USER, "/direction_single", "")  # needs an active direction
    text = bot.sent[-1][1]
    actions = inline_actions(bot)
    assert "Сначала выберите направление" in text
    assert any(a.endswith(":choose:0") for a in actions) and any(a.endswith(":choose:1") for a in actions)
    assert any(a.endswith(":new") for a in actions)


@run_async
async def test_crawled_screens_hide_internal_data_and_keep_dynamic_values(bot, monkeypatch):
    """Visible text/labels must not leak storage identities, including on drafts."""
    import json
    import re
    import telegram_bot_ui as ui
    from telegram_app import onboarding

    home = bot.profiles["client_1"].home_dir
    direction = make_direction(home, name="direction_aabbcc112233")
    direction.update(title="Электромонтажник", goal=["Мастер"], positioning="Опыт работы",
                     salary_baseline=123456, cover_letter_fallback="Моё письмо")
    # Synthetic identities only; never copy account IDs from screen captures.
    direction["variants"][0].update(title="Мастер, электрик", hh_resume_numeric_id=910000001)
    direction["variants"][1].update(title="Электрик / техник", hh_resume_numeric_id=910000002, status="paused")
    direction = directions.save_direction(home, direction, texts={})
    directions.set_active_direction(home, direction["name"])
    other = make_direction(home, name="direction_ddeeff445566")
    other["title"] = "Другое направление"
    directions.save_direction(home, other, texts={})
    monkeypatch.setattr(ui, "_active_direction_title", lambda name: direction["title"])

    # Exercise the catalog, detail, strategy, resume states, selection drafts,
    # switch confirmation and all three statistics filters without HH calls.
    await bot._dispatch_direction(1, USER, "/directions", "")
    await bot._dispatch_direction(1, USER, "/direction_summary", "")
    card = bot.sent[-1][1]
    assert all(value in card for value in ["123456 ₽", "Мастер, электрик", "Электрик / техник",
                                           "выключено", "Мастер", "Опыт работы", "Моё письмо"])
    state = bot._direction_sessions()[1]
    await bot._direction_callback(1, USER, f"dir:{state['token']}:strategy")
    for operation in ("select", "pause", "resume", "remove"):
        await bot._direction_prompt_variants(1, USER, operation)
    # Only the enabled resume is selectable; the existing Save boundary remains.
    await bot._direction_prompt_variants(1, USER, "select")
    state = bot._direction_sessions()[1]
    assert [b["callback_data"].rsplit(":", 1)[-1] for row in bot.sent[-1][2]["reply_markup"]["inline_keyboard"][:2] for b in row] == ["0", "1"]
    await bot._direction_callback(1, USER, f"dir:{state['token']}:variant:0")
    assert directions.active_direction(home) == direction
    assert "Не сохранено: откликаться только резюме «Мастер, электрик»" in bot.sent[-1][1]
    await bot._dispatch_direction(1, USER, "/directions", "")
    state = bot._direction_sessions()[1]
    index = next(i for i, d in enumerate(state["choices"]) if d["name"] == other["name"])
    await bot._direction_callback(1, USER, f"dir:{state['token']}:choose:{index}")
    assert "вместо «Электромонтажник»" in bot.sent[-1][1]

    (Path(home) / "negotiation_snapshots.jsonl").write_text(json.dumps({
        "id": 100, "resumeId": 910000001, "initialTopicType": "RESPONSE_BY_APPLICANT",
        "lastState": "INVITATION", "viewedByOpponent": True,
        "fetched_at": "2026-10-22T21:34:56.523609+03:00",
    }) + "\n")
    for scope in ("", "all", "unspecified"):
        await bot._dispatch_direction(1, USER, "/direction_stats", scope)

    texts = []
    for _, text, kwargs in bot.sent:
        texts.append(text)
        markup = kwargs.get("reply_markup", {})
        for key in ("keyboard", "inline_keyboard"):
            texts += [button["text"] for row in markup.get(key, []) for button in row]
    for role in (ui.ROLE_USER, ui.ROLE_ADMIN):
        texts += [ui.build_help_text(role, profile_name="client_1", help_section=help_section)
                  for help_section in (False, True)]
        for menu in (ui.MENU_HH, ui.MENU_CANDIDATE, ui.MENU_MONITOR, ui.MENU_SEARCH, ui.MENU_RESPONSES):
            texts.append(ui.build_menu_section_text(menu, role=role, profile_name="client_1"))
            markup = ui.build_reply_markup(role, menu=menu)
            texts += [b["text"] for row in markup["keyboard"] for b in row]
    texts += [ui.build_hh_resumes_text("client_1", [{"id": "910000001", "path": str(Path(home) / "resume.md"), "title": "Мастер, электрик"}]),
              ui.build_hh_resumes_text("client_1", [{"id": "910000002"}]),
              ui.build_hh_response_count_text({"profile": "client_1", "total": 100, "fetched_at": "2026-10-22T21:34:56.523609+03:00"}, home_dir=home),
              ui.build_hh_response_count_text({"profile": "client_1", "total": 100, "fetched_at": "2026-10-22T21:34:56.523609+03:00"})]
    for mode in ("confirm", "auto", "preview"):
        text, rows, _ = onboarding.build(hh_ok=True, resumes=[{"title": v["title"]} for v in direction["variants"]],
                                         directions=[direction, other], active=direction, mode=mode, has_search=True)
        texts += [text] + [label for row in rows for label, _ in row]
    visible = "\n".join(texts)
    assert not re.search(r"direction_[0-9a-f]+|\b(?:single|active)\b|\b91000000[12]\b|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|(?:/home/|/mnt/|[A-Z]:\\\\)", visible)
    assert "21:34" in visible and "22 октября" in visible
    assert "Мастер, электрик" in visible and "Электрик / техник" in visible


@run_async
async def test_unsaved_single_description_names_first_enabled_resume(bot):
    home = bot.profiles["client_1"].home_dir
    direction = make_direction(home, strategy="single")
    direction["variants"][0]["status"] = "paused"
    directions.save_direction(home, direction, texts={})
    directions.set_active_direction(home, direction["name"])
    await bot._dispatch_direction(1, USER, "/direction_edit", "salary")
    await bot._accept_direction_input(1, USER, "654321")
    text = bot.sent[-1][1]
    assert text.startswith('Не сохранено: откликаться только резюме «Resume 2»')
    assert "Resume 1 · выключено" in text and "Resume 2 · включено" in text
    assert directions.active_direction(home) == direction
