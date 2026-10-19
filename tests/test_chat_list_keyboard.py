"""B15: tapping «🤖 N» in the multi-chat list must not wipe the other chats' buttons."""
from telegram_app.callbacks import _lists_several_chats


def message(*callbacks):
    return {"reply_markup": {"inline_keyboard": [[{"text": str(i), "callback_data": c}] for i, c in enumerate(callbacks)]}}


def test_list_of_several_chats_keeps_its_keyboard():
    assert _lists_several_chats(message("chat_ai:qa:1:10", "chat_ai_any:qa:2:20", "gform_preview:qa:3:30"))


def test_single_chat_card_is_cleared_as_before():
    assert not _lists_several_chats(message("chat_ai:qa:1:10"))
    assert not _lists_several_chats(message("chat_ai:qa:1:10", "chat_ai_alt:qa:1:10"))
    assert not _lists_several_chats({})
