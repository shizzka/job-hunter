"""Frozen callback outcomes recorded before splitting the callback router.

The real admission/access/claim decorators run. Only downstream operations are
synthetic boundaries: no HH, network, profile file, or client registry writes.
Each case replays the same button, a copied keyboard, and a fresh bot against the
same persisted control state. The JSON is committed evidence, never regenerated
by a test. Opaque tokens rejected by downstream screens are represented by
explicit stale-boundary results, not by running those screens' own routers.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import directions
import company_blacklist
import hh_guard
import manual_apply_queue
import profile as profiles
import profile_access
import telegram_access
from telegram_app import command_queue, guards, history
from telegram_app.callbacks import TelegramCallbackRouter

SNAPSHOT = Path(__file__).with_name("fixtures") / "telegram_callbacks.json"
INVENTORY = json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


class CallbackBot(TelegramCallbackRouter):
    def __init__(self, state, events, options):
        self.state, self.events, self.options = state, events, options

    def _update_state(self, mutate):
        mutate(self.state)
        self.events.append(["state", deepcopy(self.state)])

    def _profile_names(self):
        return ["qa-dev", "other"]

    def _selected_profile(self, principal):
        return "qa-dev"

    def _profile(self, name):
        return SimpleNamespace(home_dir="/synthetic/qa-dev")

    def _commands_stopping(self):
        return self.options.get("stopping", False)

    async def _admit_command(self, entry):
        self.events.append(["admit", entry.profile, entry.chat_id, entry.user_id, entry.label,
                            plain(entry.request_key), bool(entry.source.get("_guard_claim"))])
        if self.options.get("admission_rejected"):
            return False
        token = command_queue._invocation.set((asyncio.current_task(), entry))
        try:
            result = await entry.invoke(True)
            self.events.append(["invoked", result])
        finally:
            command_queue._invocation.reset(token)
            guards.release_entry(self, entry)
        return True

    def _menu_reply_markup(self, principal):
        return {"keyboard": [[{"text": principal["role"] + " menu"}]], "resize_keyboard": True}

    def _set_selected_profile(self, *args):
        self.events.append(["_set_selected_profile", list(args), {}])

    def _append_chat_ai_audit_event(self, *args, **kwargs):
        self.events.append(["_append_chat_ai_audit_event", list(args), kwargs])

    def _client_record(self, uid):
        self.events.append(["_client_record", [uid], {}])
        status = self.options.get("client_status", "approved")
        return None if status == "missing" else {"user_id": uid, "status": status, "profile_name": "qa-dev"}

    def __getattr__(self, name):
        if not name.startswith("_"):
            raise AttributeError(name)
        async def boundary(*args, **kwargs):
            # Verify that the actual scoped_request decorator binds every call.
            scope = list(profile_access.current_scope())
            self.events.append([name, plain(args), plain(kwargs), scope])
            if name == "_show_uncertain_manual_apply":
                return self.options.get("uncertain", False)
            if name == "_form_callback" and self.options.get("form_error"):
                raise ValueError("Форма устарела: откройте новый preview.")
            if name in {"_approve_client", "_reject_client"}:
                return not self.options.get("client_failure", False)
            if name in {"_direction_callback", "_chat_owner_callback", "_form_callback", "_answer_employer_question"}:
                if self.options.get("stale"):
                    await self._send_text(args[0], "Synthetic downstream stale token")
                return "boundary-result"
            return None
        return boundary


def record_case(case, monkeypatch):
    options = case.get("options", {})
    role = case.get("role", "admin")
    principal = None if role == "guest" else {"user_id": 42, "role": role, "profile": "qa-dev", "direction": "qa-dev"}
    monkeypatch.setattr(telegram_access, "resolve_user", lambda uid: principal)
    # Tester loading is a real access-wrapper boundary. Prepared and unprepared
    # profiles exercise both outcomes without reading any real profile.
    def load_profile(name):
        if options.get("tester_unprepared"):
            raise ValueError("Synthetic unprepared tester")
        return SimpleNamespace(name=name)
    monkeypatch.setattr(profiles, "load_profile", load_profile)
    monkeypatch.setattr(config, "JOB_HUNTER_INSTANCE_KIND", "dev")
    monkeypatch.setattr(profiles, "profile_env_path", lambda name: __file__)
    monkeypatch.setattr(directions, "load_direction", lambda *args: {})
    monkeypatch.setattr(history, "token", lambda home: "current")
    monkeypatch.setattr(guards.time, "time", lambda: 1800000000.0)
    state, events = {}, []
    candidate = {"token": "new-token", "vacancy": {"company": "Synthetic Ltd", "id": "123"},
                 "status": "uncertain", "orphan_recovered": True, "allow_ai_apply": True}
    def operation(name, result):
        def called(*args, **kwargs):
            events.append([name, plain(args), plain(kwargs)])
            return deepcopy(result)
        return called
    missing = options.get("missing_item")
    if options.get("no_company"):
        candidate["vacancy"] = {}
    if options.get("resolved_item"):
        candidate["status"] = "handled"
    for name in ("record_feedback", "snooze_candidate", "get_candidate"):
        monkeypatch.setattr(manual_apply_queue, name, operation(name, None if missing else candidate))
    monkeypatch.setattr(manual_apply_queue, "allow_stub_cover", operation("allow_stub_cover", not missing))
    monkeypatch.setattr(manual_apply_queue, "resolve_orphaned_apply", operation("resolve_orphaned_apply", None if options.get("resolve_failure") else candidate))
    monkeypatch.setattr(manual_apply_queue, "mark_candidate", operation("mark_candidate", candidate))
    monkeypatch.setattr(manual_apply_queue, "build_manual_why_text", operation("build_manual_why_text", "Synthetic explanation"))
    monkeypatch.setattr(manual_apply_queue, "build_manual_apply_markup", operation("build_manual_apply_markup", {"inline_keyboard": [[{"text": "Apply", "callback_data": "manual_apply:qa-dev:token"}]]}))
    monkeypatch.setattr(company_blacklist, "set_blocked", operation("set_blocked", None))
    def cooldown(name):
        events.append(["clear_cooldown_for_profile", [name], {}])
        if options.get("cooldown_failure"):
            raise RuntimeError("Synthetic cooldown failure")
    monkeypatch.setattr(hh_guard, "clear_cooldown_for_profile", cooldown)
    bot = CallbackBot(state, events, options)
    callback = {"id": "callback", "from": {"id": 42}, "data": case["data"],
                "message": {"message_id": 7, "chat": {"id": 42, "type": case.get("chat_type", "private")}}}
    if options.get("no_message_id"):
        callback["message"].pop("message_id")
    if options.get("no_chat"):
        callback["message"] = {}
    if options.get("multi_chat"):
        callback["message"]["reply_markup"] = {"inline_keyboard": [[
            {"text": "one", "callback_data": "chat_ai:qa-dev:10:20"},
            {"text": "two", "callback_data": "chat_ai_any:qa-dev:11:21"}]]}
    async def run():
        outcomes = []
        for phase in ("first", "repeat", "copied", "restart"):
            nonlocal bot
            if phase == "copied":
                callback["message"]["message_id"] = 8
            if phase == "restart":
                bot = CallbackBot(state, events, options)
            events.clear()
            result = await bot._handle_callback_query(deepcopy(callback))
            outcomes.append({"phase": phase, "return": result, "events": deepcopy(events), "state": deepcopy(state)})
        return outcomes
    return asyncio.run(run())


def f27_review_contract(case, expected):
    """Explicit intentional UX deltas over the frozen split22 evidence.

    Preserve the original inventory verbatim. Every phase still compares exact
    claims, admission, access scopes and operations; only the named review
    outcomes change to the F27 pending-only contract.
    """
    expected = [deepcopy(outcome) for outcome in expected]
    name = case["name"]
    applies = {"apply", "apply_user", "apply_tester", "apply_group_admin",
               "admission_stopping", "apply_no_message"}
    feedbacks = {"feedback_good", "feedback_bad", "feedback_good_user",
                 "feedback_failed", "feedback_good_no_message"}
    blocks = {"block", "block_user", "block_failed", "block_no_company"}
    for outcome in expected:
        events = outcome["events"]
        if name == "review_unknown":
            outcome["state"] = {}
            outcome["events"] = [
                ["get_candidate", ["2"], {"profile_name": "qa-dev"}],
                ["_answer_callback_query", ["callback", "Карточка уже обработана: результат не подтверждён, проверьте на hh.ru."], {}, ["", "", ""]]]
        elif name in applies and any(e[0] == "_start_manual_ai_apply" for e in events):
            updated = []
            for event in events:
                if event[0] == "_start_manual_ai_apply":
                    continue
                if event[0] == "_answer_callback_query" and event[1][1:] == ["Отправляю отклик через ИИ…"]:
                    updated.extend([
                        ["get_candidate", ["token"], {"profile_name": "qa-dev"}],
                        [event[0], ["callback", "Карточка уже обработана: нужна сверка на hh.ru."], {"show_alert": True}, event[3]]])
                else:
                    updated.append(event)
            outcome["events"] = updated
        elif name in feedbacks and any(e[0] == "record_feedback" for e in events):
            start = next(i for i, e in enumerate(events) if e[0] == "record_feedback")
            scope = next(e[3] for e in events if e[0] == "_answer_callback_query")
            state = "не найдена" if name == "feedback_failed" else "нужна сверка на hh.ru"
            outcome["events"] = events[:start] + [
                ["get_candidate", ["token"], {"profile_name": "qa-dev"}],
                ["_answer_callback_query", ["callback", f"Карточка уже не ждёт решения: {state}."], {"show_alert": True}, scope]]
        elif name in blocks and any(e[0] == "get_candidate" for e in events):
            start = next(i for i, e in enumerate(events) if e[0] == "get_candidate")
            scope = next(e[3] for e in events if e[0] == "_answer_callback_query")
            missing = name == "block_failed"
            outcome["events"] = events[:start + 1] + [
                ["_answer_callback_query", ["callback", "Карточка недоступна. Обновите список." if missing else
                    "Карточка уже обработана: результат не подтверждён, проверьте на hh.ru."],
                    {"show_alert": True} if missing else {}, scope]]
        elif name == "block_foreign":
            for event in events:
                if event[0] == "_answer_callback_query" and event[1][1:] != ["Уже сделано"]:
                    event[1] = ["callback", "Карточка недоступна."]
                    event[2] = {"show_alert": True}
    # Replaying a spent review action now reads its saved state; claims stay
    # identical to split22 and the action itself is never retried.
    parts = case["data"].split(":")
    profile = parts[1] if len(parts) >= 3 else ""
    card_protocol = parts[0] in {"manual_apply", "manual_fb", "manual_snooze", "manual_block_company", "stub", "mad"}
    allowed = case.get("role", "admin") == "admin" or (profile == "qa-dev" and case.get("chat_type", "private") == "private")
    if card_protocol and profile in {"qa-dev", "other"} and allowed:
        for outcome in expected:
            updated = []
            for event in outcome["events"]:
                if event[0] == "_answer_callback_query" and event[1][1:] == ["Уже сделано"]:
                    state = "не ждёт решения" if case.get("options", {}).get("missing_item") else "нужна сверка на hh.ru"
                    updated.append(["get_candidate", [parts[2]], {"profile_name": profile}])
                    event[1][1] = f"Карточка: {state}."
                updated.append(event)
            outcome["events"] = updated
    if case["name"] in {"why", "why_user"}:
        for outcome in expected:
            for event in outcome["events"]:
                if event[0] == "_answer_callback_query" and event[1][1:] == ["Показываю причину"]:
                    event[1][1] = "Карточка: нужна сверка на hh.ru. Показываю причину."
    return expected


@pytest.mark.parametrize("case", INVENTORY["cases"], ids=lambda case: case["name"])
def test_callback_outcome(case, monkeypatch):
    expected = [{"phase": phase, **INVENTORY["traces"][index]}
        for phase, index in zip(("first", "repeat", "copied", "restart"),
                                INVENTORY["outcomes"][case["name"]], strict=True)]
    assert record_case(case, monkeypatch) == f27_review_contract(case, expected)
