"""Area handlers for the stable Telegram callback protocol."""
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class CallbackContext:
    callback_id: str
    user_id: int
    principal: dict
    raw_data: str
    message: dict
    chat_id: int
    message_id: int
    can_access_profile: Callable[[str], bool]
