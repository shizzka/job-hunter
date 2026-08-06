import asyncio

import pytest

import telegram_bot


def _callback(
    data: str,
    *,
    user_id: int = 1,
    chat_id: int = 42,
    message_id: int = 7,
) -> dict:
    return {
        "id": "callback-1",
        "from": {"id": user_id},
        "data": data,
        "message": {
            "chat": {"id": chat_id},
            "message_id": message_id,
        },
    }


def _configured_bot(monkeypatch, principal=None):
    if principal is None:
        principal = {
            "user_id": 1,
            "role": telegram_bot.ROLE_ADMIN,
            "profile": "qa",
        }
    bot = telegram_bot.TelegramBot("qa")
    calls = {
        "answers": [],
        "edits": [],
        "messages": [],
    }

    monkeypatch.setattr(
        telegram_bot.telegram_access,
        "resolve_user",
        lambda user_id: principal,
    )
    monkeypatch.setattr(bot, "_profile_names", lambda: ["qa"])
    monkeypatch.setattr(
        bot,
        "_selected_profile",
        lambda current_principal: "qa",
    )
    monkeypatch.setattr(
        bot,
        "_menu_reply_markup",
        lambda current_principal: {"menu": True},
    )

    async def answer_callback(
        callback_id,
        text="",
        *,
        show_alert=False,
    ):
        calls["answers"].append(
            (callback_id, text, show_alert)
        )

    async def edit_reply_markup(
        chat_id,
        message_id,
        *,
        reply_markup=None,
    ):
        calls["edits"].append(
            (chat_id, message_id, reply_markup)
        )

    async def send_text(
        chat_id,
        text,
        *,
        reply_markup=None,
    ):
        calls["messages"].append(
            (chat_id, text, reply_markup)
        )
        return {}

    monkeypatch.setattr(
        bot,
        "_answer_callback_query",
        answer_callback,
    )
    monkeypatch.setattr(
        bot,
        "_edit_reply_markup",
        edit_reply_markup,
    )
    monkeypatch.setattr(bot, "_send_text", send_text)
    return bot, calls


@pytest.mark.parametrize(
    ("principal", "expected_text"),
    [
        (None, "🔒 Доступ закрыт."),
        (
            {"user_id": 2, "role": telegram_bot.ROLE_USER, "profile": "qa"},
            "🔒 Нужны права администратора.",
        ),
    ],
)
def test_callback_router_enforces_admin_access(
    principal,
    expected_text,
    monkeypatch,
):
    bot = telegram_bot.TelegramBot("qa")
    answers = []
    monkeypatch.setattr(
        telegram_bot.telegram_access,
        "resolve_user",
        lambda user_id: principal,
    )

    async def answer(
        callback_id,
        text="",
        *,
        show_alert=False,
    ):
        answers.append((callback_id, text, show_alert))

    monkeypatch.setattr(bot, "_answer_callback_query", answer)

    asyncio.run(
        bot._handle_callback_query(
            _callback("hh_reauth:qa", user_id=2)
        )
    )

    assert answers == [
        ("callback-1", expected_text, True)
    ]


def test_callback_router_rejects_missing_chat(monkeypatch):
    bot, calls = _configured_bot(monkeypatch)

    asyncio.run(
        bot._handle_callback_query(
            _callback("hh_reauth:qa", chat_id=0)
        )
    )

    assert calls["answers"] == [
        (
            "callback-1",
            "Не удалось определить чат.",
            True,
        )
    ]


def test_callback_router_starts_hh_reauth(monkeypatch):
    bot, calls = _configured_bot(monkeypatch)
    starts = []

    async def start_hh_auth(
        chat_id,
        principal,
        *,
        profile_name,
    ):
        starts.append(
            (chat_id, principal["user_id"], profile_name)
        )

    monkeypatch.setattr(
        bot,
        "_start_profile_hh_auth_capture",
        start_hh_auth,
    )

    asyncio.run(
        bot._handle_callback_query(
            _callback("hh_reauth:qa")
        )
    )

    assert calls["answers"] == [
        ("callback-1", "Запускаю вход HH…", False)
    ]
    assert calls["edits"] == [(42, 7, None)]
    assert starts == [(42, 1, "qa")]


def test_callback_router_starts_google_form_actions(monkeypatch):
    bot, calls = _configured_bot(monkeypatch)
    previews = []
    submits = []

    async def start_preview(
        chat_id,
        principal,
        *,
        profile_name,
        hh_chat_id,
        hh_message_id,
    ):
        previews.append(
            (
                chat_id,
                profile_name,
                hh_chat_id,
                hh_message_id,
            )
        )

    async def start_submit(
        chat_id,
        principal,
        *,
        profile_name,
        token,
    ):
        submits.append((chat_id, profile_name, token))

    monkeypatch.setattr(
        bot,
        "_start_google_form_preview",
        start_preview,
    )
    monkeypatch.setattr(
        bot,
        "_start_google_form_submit",
        start_submit,
    )

    asyncio.run(
        bot._handle_callback_query(
            _callback("gform_preview:qa:5416682595:14513855732")
        )
    )
    asyncio.run(
        bot._handle_callback_query(
            _callback("gform_submit:qa:abcdef123456")
        )
    )

    assert previews == [
        (42, "qa", "5416682595", "14513855732")
    ]
    assert submits == [
        (42, "qa", "abcdef123456")
    ]
    assert [item[1] for item in calls["answers"]] == [
        "Готовлю Google Form preview…",
        "Отправляю Google Form…",
    ]


@pytest.mark.parametrize(
    ("data", "force_send", "allow_any", "answer_text"),
    [
        (
            "chat_ai:qa:5416682595:14513855732",
            False,
            False,
            "Генерирую ответ через ИИ…",
        ),
        (
            "chat_send:qa:5416682595:14513855732",
            True,
            False,
            "Отправляю ответ в HH…",
        ),
        (
            "chat_ai_any:qa:5416682595:14513855732",
            False,
            True,
            "Генерирую ответ через ИИ…",
        ),
        (
            "chat_send_any:qa:5416682595:14513855732",
            True,
            True,
            "Отправляю ответ в HH…",
        ),
    ],
)
def test_callback_router_starts_chat_ai_action(
    data,
    force_send,
    allow_any,
    answer_text,
    monkeypatch,
):
    bot, calls = _configured_bot(monkeypatch)
    starts = []
    audits = []

    async def start_chat_ai(
        chat_id,
        principal,
        *,
        profile_name,
        hh_chat_id,
        hh_message_id,
        force_send=False,
        allow_any=False,
    ):
        starts.append(
            {
                "chat_id": chat_id,
                "profile_name": profile_name,
                "hh_chat_id": hh_chat_id,
                "hh_message_id": hh_message_id,
                "force_send": force_send,
                "allow_any": allow_any,
            }
        )

    monkeypatch.setattr(
        bot,
        "_start_chat_ai_reply",
        start_chat_ai,
    )
    monkeypatch.setattr(
        bot,
        "_append_chat_ai_audit_event",
        lambda event, **payload: audits.append(
            (event, payload)
        ),
    )

    asyncio.run(
        bot._handle_callback_query(_callback(data))
    )

    assert starts == [
        {
            "chat_id": 42,
            "profile_name": "qa",
            "hh_chat_id": "5416682595",
            "hh_message_id": "14513855732",
            "force_send": force_send,
            "allow_any": allow_any,
        }
    ]
    assert calls["answers"] == [
        ("callback-1", answer_text, False)
    ]
    assert audits[0][0] == "callback"
    assert audits[0][1]["action"] == (
        "send" if force_send else "preview"
    )


def test_callback_router_starts_manual_apply(monkeypatch):
    bot, calls = _configured_bot(monkeypatch)
    starts = []

    async def start_apply(
        chat_id,
        principal,
        *,
        profile_name,
        token,
    ):
        starts.append((chat_id, profile_name, token))

    monkeypatch.setattr(
        bot,
        "_start_manual_ai_apply",
        start_apply,
    )

    asyncio.run(
        bot._handle_callback_query(
            _callback("manual_apply:qa:token-1")
        )
    )

    assert starts == [(42, "qa", "token-1")]
    assert calls["answers"] == [
        (
            "callback-1",
            "Отправляю отклик через ИИ…",
            False,
        )
    ]


def test_callback_router_records_manual_feedback(monkeypatch):
    bot, calls = _configured_bot(monkeypatch)
    feedback_calls = []
    item = {"vacancy": {"id": "1"}}

    monkeypatch.setattr(
        telegram_bot.manual_apply_queue,
        "record_feedback",
        lambda token, feedback, *, user_id: (
            feedback_calls.append(
                (token, feedback, user_id)
            )
            or item
        ),
    )
    monkeypatch.setattr(
        telegram_bot.manual_apply_queue,
        "feedback_label",
        lambda feedback: "Полезно",
    )
    monkeypatch.setattr(
        telegram_bot.manual_apply_queue,
        "build_manual_apply_markup",
        lambda vacancy, profile_name, token, *, include_feedback: {
            "manual": token,
        },
    )

    asyncio.run(
        bot._handle_callback_query(
            _callback("manual_fb:qa:token-1:good")
        )
    )

    assert feedback_calls == [("token-1", "good", 1)]
    assert calls["answers"] == [
        ("callback-1", "Записал: Полезно", False)
    ]
    assert calls["edits"] == [
        (42, 7, {"manual": "token-1"})
    ]


def test_callback_router_shows_manual_reason(monkeypatch):
    bot, calls = _configured_bot(monkeypatch)
    item = {"decision": {"reason": "match"}}
    monkeypatch.setattr(
        telegram_bot.manual_apply_queue,
        "get_candidate",
        lambda token: item,
    )
    monkeypatch.setattr(
        telegram_bot.manual_apply_queue,
        "build_manual_why_text",
        lambda current_item: "Причина",
    )

    asyncio.run(
        bot._handle_callback_query(
            _callback("manual_why:qa:token-1")
        )
    )

    assert calls["answers"] == [
        ("callback-1", "Показываю причину", False)
    ]
    assert calls["messages"] == [
        (42, "Причина", {"menu": True})
    ]


def test_callback_router_blocks_retry_company(monkeypatch):
    bot, calls = _configured_bot(monkeypatch)
    marks = []
    monkeypatch.setattr(
        telegram_bot.manual_apply_queue,
        "get_candidate",
        lambda token: {
            "vacancy": {"company": "Example"}
        },
    )
    monkeypatch.setattr(
        telegram_bot.manual_apply_queue,
        "mark_candidate",
        lambda token, status, reason: marks.append(
            (token, status, reason)
        ),
    )

    async def run_command_capture(argv, *, timeout):
        return {"ok": True}

    monkeypatch.setattr(
        telegram_bot.runtime_control,
        "run_command_capture",
        run_command_capture,
    )

    asyncio.run(
        bot._handle_callback_query(
            _callback(
                "manual_block_company:qa:token-1"
            )
        )
    )

    assert marks == [
        (
            "token-1",
            "company_blocked",
            "retry company blocked: Example",
        )
    ]
    assert calls["messages"][0][1] == (
        "🛑 Retry-отклики в компанию Example "
        "отключены для профиля qa."
    )


def test_callback_router_retries_captcha_search(monkeypatch):
    import hh_guard

    bot, calls = _configured_bot(monkeypatch)
    starts = []
    cooldown_clears = []
    monkeypatch.setattr(
        hh_guard,
        "clear_cooldown",
        lambda: cooldown_clears.append(True),
    )

    async def start_cli(
        chat_id,
        principal,
        profile_name,
        flag,
        label,
        timeout,
    ):
        starts.append(
            (
                chat_id,
                profile_name,
                flag,
                label,
                timeout,
            )
        )

    monkeypatch.setattr(
        bot,
        "_start_cli_command",
        start_cli,
    )

    asyncio.run(
        bot._handle_callback_query(
            _callback("captcha_retry:request-1")
        )
    )

    assert cooldown_clears == [True]
    assert starts == [
        (42, "qa", "--search", "search", 3600)
    ]
    assert calls["answers"] == [
        (
            "callback-1",
            "🔁 Запускаю поиск заново…",
            False,
        )
    ]


def test_callback_router_handles_client_approval(monkeypatch):
    bot, calls = _configured_bot(monkeypatch)
    approvals = []

    async def approve(
        chat_id,
        principal,
        target_user_id,
        *,
        profile_name="",
    ):
        approvals.append(
            (chat_id, target_user_id, profile_name)
        )
        return {"user_id": target_user_id}

    monkeypatch.setattr(bot, "_approve_client", approve)

    asyncio.run(
        bot._handle_callback_query(
            _callback("ca:42")
        )
    )

    assert approvals == [(42, 42, "")]
    assert calls["answers"] == [
        ("callback-1", "Одобряю клиента…", False)
    ]
    assert calls["edits"] == [(42, 7, None)]


def test_callback_router_rejects_unknown_action(monkeypatch):
    bot, calls = _configured_bot(monkeypatch)

    asyncio.run(
        bot._handle_callback_query(
            _callback("unknown:42")
        )
    )

    assert calls["answers"] == [
        (
            "callback-1",
            "Неизвестное действие.",
            True,
        )
    ]
