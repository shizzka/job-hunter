"""Frozen pre-split dispatch outcomes on synthetic profiles.

The inventory includes slash aliases, legacy/guest keyboard labels (including
labels unavailable to a role), and generated profile buttons. Real dispatch and
handlers run; only process, network and AI boundaries are replaced. Snapshots
deduplicate identical outcomes and retain full texts, first lines, keyboard
layouts, handler calls, return values and state writes. Do not regenerate them
to accommodate a refactor.
"""
import asyncio
from functools import wraps
import inspect
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import directions
import hh.session_probe
import profile as profiles
import telegram_access
import telegram_bot
import telegram_bot_ui as ui
from tests.direction_view_fakes import make_direction
from tests.telegram_f27_expectations import f27_expected
from tests.test_telegram_menu_routing import menu_bot  # noqa: F401


SNAPSHOT = Path(__file__).with_name("fixtures") / "telegram_dispatch.json"
INVENTORY = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
# The original synthetic missing-cookie error names the recording checkout.
# Keep the frozen data intact while comparing the same OS error on workers.
RECORDED_CHECKOUT = "/mnt/work/jh-stab"
CURRENT_CHECKOUT = str(Path(telegram_bot.__file__).resolve().parent)
ROLES = ("admin", "user", "tester")
BOUND_TESTER_SCENARIOS = {"bound_command", "bound_button"}
HANDLERS = (
    "_send_menu", "_send_getting_started", "_send_busy_status", "_cancel_active_command",
    "_dispatch_direction", "_show_review_queue", "_finish_facts_upload",
    "_send_candidate_question", "_candidate_advance", "_list_forms", "_save_search_setting",
    "_request_application_mode", "_start_search_query_suggestion",
    "_start_profile_hh_auth_capture", "_request_hh_auth", "_start_hh_auth_capture",
    "_approve_client", "_reject_client", "_start_market_research", "_send_run_summary",
    "_send_log_tail", "_start_chat_ai_candidate_list", "_start_chat_ai_reply", "_start_cli_command",
)


def cases():
    for role in ROLES:
        for command in INVENTORY["commands"]:
            yield role, "command", command, ""
        for label in INVENTORY["labels"]:
            yield role, "button", label, ""
        for scenario, command, arg in INVENTORY["branches"]:
            yield role, scenario, command, arg
    # Keep the original unprepared-tester rejections as separate coverage.
    for command in INVENTORY["commands"]:
        yield "tester", "bound_command", command, ""
    for label in INVENTORY["labels"]:
        yield "tester", "bound_button", label, ""


def case_key(role, scenario, text, arg):
    return json.dumps([role, scenario, text, arg], ensure_ascii=False)


@pytest.fixture
def dispatch_bot(menu_bot, monkeypatch, tmp_path):
    bot, workers = menu_bot
    monkeypatch.setattr(config, "JOB_HUNTER_INSTANCE_KIND", "dev")
    home = Path(config.JOB_HUNTER_HOME) / "profiles" / "qa-dev"
    home.mkdir(parents=True)
    (home / "profile.env").write_text("HH_ENABLED=0\n", encoding="utf-8")
    (home / "resume.md").write_text("Synthetic resume", encoding="utf-8")
    profile = profiles.Profile(name="qa-dev", home_dir=str(home), resume_file=str(home / "resume.md"))
    make_direction(home)
    monkeypatch.setattr(bot, "_profile", lambda name: profile)
    monkeypatch.setattr(bot, "_profile_names", lambda: ["qa-dev"])
    monkeypatch.setattr(bot, "_selected_profile", lambda principal: "qa-dev")
    monkeypatch.setattr(bot, "_profile_schedule", lambda name: (480, 60))
    monkeypatch.setattr(bot, "_active_elapsed_sec", lambda command: 0)
    monkeypatch.setattr(bot, "_log_tail", lambda *a, **kw: ("", ""))
    monkeypatch.setattr(bot, "_recent_runs", lambda *a, **kw: [])
    monkeypatch.setattr(bot, "_append_debug_log", lambda *a, **kw: None)
    monkeypatch.setattr(bot, "_append_chat_ai_audit_event", lambda *a, **kw: None)
    monkeypatch.setattr(telegram_bot.runtime_control, "find_agent_pids_for_profile", lambda name: [])
    monkeypatch.setattr(hh.session_probe, "probe", lambda *a: {"status": "missing", "message": "Synthetic session absent"})
    monkeypatch.setattr(telegram_bot.telegram_access, "set_impersonation", lambda *a: None)
    # Direction tokens/queue IDs are opaque identities, not routing outcomes.
    import uuid
    monkeypatch.setattr(uuid, "uuid4", lambda: SimpleNamespace(hex="0123456789abcdef0123456789abcdef"))
    from telegram_app import history
    monkeypatch.setattr(history, "token", lambda home: "synthetic-token")
    events = []
    for name in HANDLERS:
        original = getattr(bot, name)
        def wrap(original, name):
            @wraps(original)
            async def called(*args, **kwargs):
                # Principal and chat are already in the case identity. Keep all
                # handler-specific arguments, including CLI flags and timeouts.
                events.append([name, list(args[2:]), kwargs])
                return await original(*args, **kwargs)
            return called
        monkeypatch.setattr(bot, name, wrap(original, name))
    writes = []
    def update_env(name, values):
        writes.append([name, values])
        return str(home / "profile.env")
    monkeypatch.setattr(profiles, "update_profile_env", update_env)
    monkeypatch.setattr(telegram_access, "upsert_user", lambda uid, **kw: writes.append(["grant", uid, kw]) or {"user_id": uid, **kw})
    monkeypatch.setattr(telegram_access, "remove_user", lambda uid: writes.append(["revoke", uid]) or True)
    return bot, workers, profile, events, writes


def normalize(value, home):
    if isinstance(value, dict):
        if {"token", "snapshot", "choices", "profile"} <= value.keys():
            # F27 launch token and path-dependent settings digest are opaque;
            # retain all profile, search selection and mode fields around them.
            value = {**value, "token": "<launch-token>", "snapshot": "<launch-snapshot>"}
        return {str(key): "<fingerprint>" if key == "base" else normalize(item, home)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item, home) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, float) and 1_000_000_000 <= value < 10_000_000_000:
        return "<epoch>"
    if isinstance(value, str):
        if re.fullmatch(r"fs:stop:[a-f0-9]+", value):
            return "fs:stop:<active-token>"
        value = value.replace(str(home), "<profile>")
        for checkout in (CURRENT_CHECKOUT, RECORDED_CHECKOUT):
            value = value.replace(checkout, "<checkout>")
        value = re.sub(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:[+-]\d\d:\d\d|Z)?", "<time>", value)
    return value


def profile_files(home):
    files = {}
    for path in Path(home).rglob("*"):
        if path.is_file() and not path.name.endswith(".lock"):
            text = path.read_text(encoding="utf-8")
            files[str(path.relative_to(home))] = json.loads(text) if path.suffix == ".json" else text
    return files


def markup_layout(markup):
    if not markup:
        return None
    # Retain layout and Telegram keyboard options; callback tokens are covered
    # by their own component tests and contain random/path-bound identities.
    return {key: [[button["text"] for button in row] for row in value]
            if key in {"keyboard", "inline_keyboard"} else value
            for key, value in markup.items()}


@pytest.mark.parametrize("role,scenario,text,arg", list(cases()))
def test_dispatch_outcome(dispatch_bot, monkeypatch, role, scenario, text, arg):
    bot, workers, profile, events, writes = dispatch_bot
    principal = {"user_id": 42, "role": role, "profile": "qa-dev", "direction": "qa"}
    # Deferred output now authorizes the real registry, not just resolve_user.
    # Match the synthetic request without bypassing either delivery guard.
    telegram_access.save_registry({"users": [principal]})
    monkeypatch.setattr(telegram_access, "resolve_user", lambda uid: principal)
    command = text
    is_button = scenario in {"button", "bound_button"}
    if is_button:
        command, arg = ui._resolve_message_command(text, role)
    if scenario in {"active_direction", "active_busy"} | BOUND_TESTER_SCENARIOS:
        directions.set_active_direction(profile.home_dir, "qa")
    if scenario in BOUND_TESTER_SCENARIOS:
        # Exercise the actual tester admission checks before recording outcomes.
        # A bad fixture must fail here rather than freeze another refusal matrix.
        with telegram_bot.profile_access.principal_scope(principal):
            assert profiles.load_profile("qa-dev").direction_name == "qa"
        # Only external HH/AI boundaries are replaced; routing and access guards
        # remain real. These paths were unreachable in the unprepared matrix.
        monkeypatch.setattr(bot, "_direction_download", AsyncMock(return_value=[{
            "hh_resume_hash": "resume_111", "hh_resume_numeric_id": 111,
            "title": "QA Engineer", "raw": "Synthetic resume 111",
        }]))
        monkeypatch.setattr(telegram_bot.search_query_suggester, "suggest_direction",
                            AsyncMock(return_value={"search_queries": ["Synthetic QA"]}))
        from hh import negotiation_snapshot
        monkeypatch.setattr(negotiation_snapshot, "collect", lambda **kw: {
            "filter": "archived" if kw.get("archived") else "active",
            "complete": True, "pages_read": 0, "stop_reason": None,
        })
    if scenario == "pending_direction":
        bot._direction_sessions()[42] = {"profile_name": "qa-dev"}
    if scenario == "impersonating":
        principal["impersonating"] = True
    if scenario in {"busy", "active_busy", "core_busy"}:
        active = SimpleNamespace(label="synthetic busy", owner_user_id=42,
                                 profile_name="qa-dev", subprocess_pid=0, task=None, started_at=123.0)
        monkeypatch.setattr(bot, "_active_command", lambda name: active)
        monkeypatch.setattr(bot, "_has_active_command", lambda name: True)
    if scenario == "launching":
        bot._queue_launching["qa-dev"] = SimpleNamespace(profile="qa-dev", label="synthetic launching",
                                                       chat_id=42, user_id=42, request_key=None,
                                                       source=None, direction="qa")
    if scenario == "queued":
        monkeypatch.setattr(bot, "_queue_length", lambda name: 1)
    if scenario == "llm_cooldown":
        bot._llm_test_next_at = float("inf")
    if scenario.startswith("candidate_"):
        bot._set_candidate_state(42, {"profile_name": "qa-dev", "kind": "add", "mode": "confirm",
                                      "pending_text": "Synthetic fact", "pending_topic": "общий"})
    if scenario == "draft":
        bot._set_search_settings_state(42, search_query_draft=["QA engineer"])
    if scenario == "stale_draft":
        bot._set_search_settings_state(42, search_query_draft=["QA engineer"], search_query_draft_profile="other-dev")
    if scenario == "catalog":
        monkeypatch.setattr(telegram_bot.client_hh_auth, "load_hh_resume_catalog", lambda name: [{"id": "synthetic", "title": "Synthetic QA"}])
    if scenario == "no_client":
        monkeypatch.setattr(bot, "_client_record", lambda uid: None)
    if scenario == "approved":
        monkeypatch.setattr(bot, "_client_record", lambda uid: {"user_id": uid, "profile_name": "qa-dev", "status": "approved"})
        # These browser/auth boundaries have their own integration tests.
        for name in ("_request_hh_auth", "_start_hh_auth_capture", "_start_profile_hh_auth_capture"):
            async def auth(*args, _name=name, **kwargs):
                events.append([_name, list(args[2:]), kwargs])
                await bot._send_text(42, "Synthetic HH auth", reply_markup=bot._menu_reply_markup(principal))
            monkeypatch.setattr(bot, name, auth)
    if scenario == "no_client":
        # Admin's no-client route launches the profile browser capture.
        async def capture(*args, **kwargs):
            events.append(["_start_profile_hh_auth_capture", list(args[2:]), kwargs])
            await bot._send_text(42, "Synthetic profile HH auth")
        monkeypatch.setattr(bot, "_start_profile_hh_auth_capture", capture)

    async def run():
        if scenario in {"core_busy", "launching", "queued"}:
            with telegram_bot.profile_access.principal_scope(principal):
                result = await inspect.unwrap(bot._dispatch)(bot, 42, principal, command, arg)
        else:
            result = await bot._dispatch(42, principal, command, arg)
        for worker in workers:
            if worker.task:
                await worker.task
        queue = [{"label": entry.label, "queued": entry.queued}
                 for entry in bot._command_queues.get("qa-dev", [])]
        await bot._shutdown_command_queue()
        return result, queue
    before = profile_files(profile.home_dir)
    result, queue = asyncio.run(run())
    after = profile_files(profile.home_dir)
    profile_writes = {path: after.get(path) for path in before.keys() | after.keys()
                      if before.get(path) != after.get(path)}
    replies = [{"first_line": call.args[1].splitlines()[0], "text": call.args[1],
                "markup": markup_layout(call.kwargs.get("reply_markup"))}
               for call in bot._send_text.call_args_list]
    documents = [{"filename": call.kwargs["filename"], "content": call.kwargs["content"],
                  "caption": call.kwargs.get("caption"), "markup": markup_layout(call.kwargs.get("reply_markup"))}
                 for call in bot._send_document.call_args_list]
    actual = normalize({"resolved": [command, arg], "handlers": events, "return": result,
                        "replies": replies, "documents": documents, "state": bot._load_state(),
                        "settings_writes": writes, "profile_writes": profile_writes,
                        "direction_sessions": bot._direction_sessions(), "queue": queue}, profile.home_dir)
    # Outcomes shared by slash commands and buttons are stored only once.
    if scenario in BOUND_TESTER_SCENARIOS:
        assert all(reply["first_line"] != "🔒 Профиль tester или привязанное направление не подготовлены."
                   for reply in actual["replies"])
    key = case_key(role, scenario, text, "" if is_button else arg)
    baseline = normalize(INVENTORY["outcomes"][INVENTORY["cases"][key]], profile.home_dir)
    expected = f27_expected("dispatch", key, baseline)
    assert actual == expected


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("label,command,first_line", [
    ("🔎 Найти вакансии", "/find", "🔎 Найти вакансии сейчас"),
    ("📋 Ждут решения", "/review", "✅ Всё разобрано. Сейчас от вас ничего не требуется."),
    ("⚙️ Настройки", "/menu_settings", "⚙️ Настройки"),
])
def test_f27_new_destinations_are_read_only(dispatch_bot, monkeypatch, role, label, command, first_line):
    """New labels extend the inventory without replacing its legacy evidence."""
    bot, workers, profile, events, writes = dispatch_bot
    principal = {"user_id": 42, "role": role, "profile": "qa-dev", "direction": "qa"}
    directions.set_active_direction(profile.home_dir, "qa")
    monkeypatch.setattr(telegram_access, "resolve_user", lambda uid: principal)
    assert ui._resolve_message_command(label, role) == (command, "")
    before = profile_files(profile.home_dir)
    asyncio.run(bot._dispatch(42, principal, command, ""))
    assert bot._send_text.call_args_list[0].args[1].splitlines()[0] == first_line
    assert workers == []
    assert writes == []
    assert profile_files(profile.home_dir) == before
    assert all(event[0] != "_start_cli_command" for event in events)
    if command == "/find":
        launch = bot._load_state()["user_state"]["42"]["find_launch"]
        assert launch["profile"] == "qa-dev"
        assert launch["choices"] == launch["selected"] == ["qa"]
        assert launch["auto"] is True
    elif command == "/review":
        assert events == [["_show_review_queue", [], {"profile_name": "qa-dev", "reset": True}]]
    else:
        assert bot._load_state()["user_state"]["42"]["menu"] == "settings"
