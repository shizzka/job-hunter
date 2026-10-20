"""The owner picker binds only the explicitly selected application and resume."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import chat_context
import chat_owner_review
from telegram_app.chat_ownership import TelegramChatOwnership
from telegram_app.callbacks import TelegramCallbackRouter


ADMIN = {"user_id": 7, "role": "admin"}


class Picker(TelegramChatOwnership, TelegramCallbackRouter):
    def __init__(self, homes):
        self.homes = homes
        self.sent = []
        self.selected = "qa"
        self._set_selected_profile = Mock(side_effect=AssertionError("global profile change"))
        self._start_chat_ai_reply = Mock(side_effect=AssertionError("generation"))

    def _profile_names(self):
        return list(self.homes)

    def _profile(self, name):
        return SimpleNamespace(home_dir=str(self.homes[name]), hh=SimpleNamespace(cookies_file=""))

    def _selected_profile(self, principal):
        return self.selected

    async def _send_text(self, chat_id, text, **kwargs):
        self.sent.append((text, kwargs.get("reply_markup")))

    async def _answer_callback_query(self, *args, **kwargs):
        pass


@pytest.fixture
def picker(tmp_path):
    homes = {}
    for name, resume in [("qa", "111"), ("electrician", "222")]:
        home = tmp_path / "profiles" / name
        (home / "directions").mkdir(parents=True)
        (home / "directions" / "chosen.json").write_text(json.dumps({
            "name": "chosen", "variants": [{"hh_resume_numeric_id": resume, "title": name}]}))
        homes[name] = home
    return Picker(homes)


def click(bot, data, principal=ADMIN):
    asyncio.run(bot._chat_owner_callback(7, principal, data))


def button(bot, index=0):
    return bot.sent[-1][1]["inline_keyboard"][index][0]["callback_data"]


def start(bot):
    token = chat_owner_review.create_review(str(bot.homes["qa"]), "77", "9", "qa")
    return f"cbo:qa:{token}"


def test_picker_binds_exact_application_without_generation_or_global_direction(picker, monkeypatch):
    import directions
    monkeypatch.setattr(directions, "set_active_direction", Mock(side_effect=AssertionError("global direction")))
    opening = start(picker)
    confirm = Mock(wraps=chat_owner_review.confirm_selection)
    monkeypatch.setattr(chat_owner_review, "confirm_selection", confirm)
    click(picker, opening)
    click(picker, button(picker, 1))
    assert "ИМЕННО ЭТОТ отклик" in picker.sent[-1][0]
    click(picker, button(picker))
    text, markup = picker.sent[-1]
    assert "Владелец отклика сохранён" in text
    assert [row[0]["callback_data"] for row in markup["inline_keyboard"]] == [
        "chat_ai_any:electrician:77:9", "gform_preview:electrician:77:9"]
    picker._set_selected_profile.assert_not_called()
    picker._start_chat_ai_reply.assert_not_called()
    directions.set_active_direction.assert_not_called()
    confirm.assert_called_once()
    assert confirm.call_args.kwargs["target_home"] == str(picker.homes["electrician"])
    owner = chat_context.resolve(str(picker.homes["electrician"]), "77")
    assert owner.status == chat_context.OWN
    assert owner.resume_id == "222" and owner.direction == "chosen"
    with pytest.raises(ValueError):
        chat_owner_review.read_review(str(picker.homes["qa"]), opening.split(":")[2])


def test_later_source_snapshot_conflict_invalidates_cross_profile_choice(picker):
    click(picker, start(picker))
    click(picker, button(picker, 1))
    click(picker, button(picker))
    (picker.homes["qa"] / "negotiation_snapshots.jsonl").write_text(
        json.dumps({"chatId": "77", "resumeId": "111"}) + "\n")
    assert chat_context.safe_resolve(str(picker.homes["electrician"]), "77").status == chat_context.CONFLICT

    # Opening a fresh picker from the target cannot discard the source proof.
    target = picker.homes["electrician"]
    token = chat_owner_review.create_review(target, "77", "9", "electrician")
    choice = chat_owner_review.create_selection(target, token, [{
        "stage": "resume", "profile_name": "electrician", "resume_id": "222", "direction": "chosen"}], 7)
    with pytest.raises(ValueError):
        chat_owner_review.confirm_selection(target, choice, 0, 7, target_home=target)
    assert chat_context.safe_resolve(str(target), "77").status == chat_context.CONFLICT


def test_unauthorized_source_is_rejected_before_storage(picker, monkeypatch):
    read = Mock(side_effect=AssertionError("storage access"))
    monkeypatch.setattr(chat_owner_review, "read_review", read)
    click(picker, "cbo:electrician:0123456789ab", {"user_id": 7, "role": "user"})
    read.assert_not_called()
    assert "недоступен" in picker.sent[-1][0]


def test_target_access_is_rechecked_on_every_selection(picker):
    click(picker, start(picker))
    target = button(picker, 1)
    click(picker, target, {"user_id": 7, "role": "user"})
    assert "недоступен" in picker.sent[-1][0]
    assert picker.sent[-1][1] is None


def test_target_access_is_rechecked_at_final_confirmation(picker):
    click(picker, start(picker))
    click(picker, button(picker, 1))
    final = button(picker)
    click(picker, final, {"user_id": 7, "role": "user"})
    assert "недоступен" in picker.sent[-1][0]


def test_selection_cannot_be_replayed_by_another_admin(picker):
    click(picker, start(picker))
    selection = button(picker, 1)
    click(picker, selection, {"user_id": 8, "role": "admin"})
    assert "недоступен" in picker.sent[-1][0]


def test_user_only_sees_their_permitted_profile(picker):
    click(picker, start(picker), {"user_id": 7, "role": "user"})
    assert len(picker.sent[-1][1]["inline_keyboard"]) == 1
    assert picker.sent[-1][1]["inline_keyboard"][0][0]["text"] == "qa"


def test_router_rejects_group_owner_picker(picker, monkeypatch):
    import telegram_access
    monkeypatch.setattr(telegram_access, "resolve_user", lambda user: ADMIN)
    picker._chat_owner_callback = Mock(side_effect=AssertionError("group picker"))
    asyncio.run(picker._handle_callback_query({
        "id": "1", "from": {"id": 7}, "data": start(picker),
        "message": {"message_id": 3, "chat": {"id": 7, "type": "group"}},
    }))
    picker._chat_owner_callback.assert_not_called()


def test_unknown_reply_offers_picker_for_exact_thread_without_generation(picker, monkeypatch):
    import telegram_bot
    active = SimpleNamespace(task=None)
    picker._menu_reply_markup = lambda principal: None
    picker._has_active_command = lambda profile: False
    picker._mark_active_command = lambda **kwargs: active
    picker._clear_active_command = lambda profile: None
    picker._append_chat_ai_audit_event = lambda *args, **kwargs: None
    picker._send_text_safely = picker._send_text

    async def authorize(*args, **kwargs):
        return True

    def create_task(command, runner):
        return asyncio.create_task(runner())

    picker._authorize_ai_usage = authorize
    picker._create_active_task = create_task
    picker._run_active_command_capture = Mock(side_effect=AssertionError("generation"))
    # Avoid browser, account, or real profile operations even if old refresh code returns.
    monkeypatch.setattr(chat_context, "resolve", lambda *args, **kwargs: chat_context.ChatOwner("unknown"))

    async def run():
        await telegram_bot.TelegramBot._start_chat_ai_reply.__wrapped__(
            picker, 7, ADMIN, profile_name="qa", hh_chat_id="77", hh_message_id="9~abcdefabcdef")
        await active.task

    asyncio.run(run())
    assert picker.sent[-1][0] == chat_context.UNKNOWN_TEXT
    opening = button(picker)
    assert opening.startswith("cbo:qa:")
    review = chat_owner_review.read_review(str(picker.homes["qa"]), opening.split(":")[2])
    assert str(review["chat_id"]) == "77" and str(review["message_id"]) == "9"
    picker._run_active_command_capture.assert_not_called()
