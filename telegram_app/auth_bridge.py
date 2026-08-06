"""Telegram-side bridge for HH login prompt responses."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable


def clean_hh_auth_code_text(text: str) -> str:
    return "".join(re.findall(r"\d", text or ""))


def looks_like_standalone_hh_auth_code(text: str) -> bool:
    value = (text or "").strip()
    code = clean_hh_auth_code_text(value)
    return bool(code and code == value and 4 <= len(code) <= 8)


def can_answer_hh_auth_prompt(
    principal: dict,
    pending: dict,
    *,
    admin_role: str = "admin",
) -> bool:
    profile_name = str(pending.get("profile_name") or "")
    if principal.get("role") == admin_role:
        return True
    return bool(profile_name) and (
        str(principal.get("profile") or "") == profile_name
    )


def _load_bridge():
    import hh_auth_bridge

    return hh_auth_bridge


class TelegramHHAuthBridge:
    """Accept Telegram replies for pending HH login prompts."""

    def __init__(
        self,
        *,
        admin_role: str,
        is_menu_button: Callable[[str], bool],
        logger: logging.Logger | None = None,
    ) -> None:
        self._hh_auth_admin_role = admin_role
        self._hh_auth_is_menu_button = is_menu_button
        self._hh_auth_log = logger or logging.getLogger(__name__)

    async def _maybe_accept_hh_auth_response(
        self,
        chat_id: int,
        principal: dict,
        text: str,
    ) -> bool:
        value = (text or "").strip()
        if (
            not value
            or value.startswith("/")
            or value.startswith("➡")
            or self._hh_auth_is_menu_button(value)
        ):
            return False
        try:
            bridge = _load_bridge()
            pending = bridge.peek_pending()
        except Exception as exc:
            self._hh_auth_log.debug(
                "hh auth bridge peek failed: %s",
                exc,
            )
            return False
        if not pending or not can_answer_hh_auth_prompt(
            principal,
            pending,
            admin_role=self._hh_auth_admin_role,
        ):
            return False

        kind = str(pending.get("kind") or "code").strip()
        profile_name = str(pending.get("profile_name") or "")
        if kind == "code":
            answer = clean_hh_auth_code_text(value)
            if not 4 <= len(answer) <= 8:
                await self._send_text(
                    chat_id,
                    "🔐 Жду HH SMS-код: 4-8 цифр без лишнего текста.",
                )
                return True
            label = "SMS-код HH"
        elif kind == "login":
            answer = value
            if len(answer) < 3 or len(answer) > 120:
                await self._send_text(
                    chat_id,
                    "🔐 Жду телефон или email для входа HH.",
                )
                return True
            label = "логин HH"
        else:
            return False

        try:
            bridge.write_response(str(pending["id"]), answer)
            self._append_debug_log(
                "hh_auth_response_accepted",
                user_id=principal.get("user_id"),
                profile_name=profile_name,
                kind=kind,
            )
            await self._send_text(
                chat_id,
                (
                    f"✅ Принял {label} для профиля {profile_name}. "
                    "Ввожу в браузер HH…"
                ),
            )
            return True
        except Exception as exc:
            self._hh_auth_log.warning(
                "hh auth response write failed: %s",
                exc,
            )
            await self._send_text(
                chat_id,
                f"❌ Не смог передать ответ в HH auth: {exc}",
            )
            return True
