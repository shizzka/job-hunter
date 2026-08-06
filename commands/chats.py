"""HH chat CLI command handlers."""

import json

from hh_client import HHClient


async def _stop_client(client: HHClient) -> None:
    try:
        await client.stop()
    except Exception:
        pass


async def respond_all() -> None:
    import hh_chat_responder as cr

    client = HHClient()
    try:
        summary = await cr.process_all(client)
    finally:
        await _stop_client(client)

    print("📋 Chat-respond summary:")
    print(f"  Чатов проверено: {summary.get('chats_scanned', 0)}")
    print(f"  С AI-помощником: {summary.get('with_ai', 0)}")
    print(f"  Подозрительных HR-сообщений: {summary.get('suspicious', 0)}")
    print(f"  Уведомлений на подтверждение: {summary.get('suspicious_notified', 0)}")
    print(
        "  Google Forms: "
        f"найдено {summary.get('google_forms_found', 0)} | "
        f"preview {summary.get('google_forms_prepared', 0)} | "
        f"ошибок {summary.get('google_forms_failed', 0)}"
    )
    print(f"  Подготовлено ответов: {summary.get('answers_drafted', 0)}")
    print(f"  Отправлено: {summary.get('answers_sent', 0)}")
    print(f"  Пропущено: {summary.get('skipped', 0)}")
    print(f"  Ошибок чтения: {summary.get('read_failures', 0)}")
    for detail in summary.get("details", []):
        print(f"\n  → {detail.get('vacancy')} @ {detail.get('company')} ({detail.get('chat_id')})")
        if detail.get("google_form"):
            print(f"    Google Form: {'preview готов' if detail.get('ok') else 'ошибка'}")
            print(f"    Форма: {detail.get('form_url') or '-'}")
            if detail.get("token"):
                print(f"    Токен: {detail.get('token')}")
            if not detail.get("ok"):
                print(f"    Причина: {detail.get('message') or '-'}")
            continue
        if detail.get("suspicious"):
            print(f"    Подозрительно: {detail.get('question', '')[:140]}")
            print(f"    Уведомление: {'да' if detail.get('notified') else 'нет'}")
            continue
        print(f"    AI: {detail.get('question', '')[:140]}")
        print(f"    Ответ: {detail.get('answer', '')[:140]}")
        if detail.get("dry_run"):
            print(f"    [DRY-RUN, скрин: {detail.get('preview', {}).get('screenshot_path', '-')}]")
        elif detail.get("sent"):
            print("    [SENT ✓]")


async def list_candidates(*, limit: int, max_scan: int) -> None:
    import hh_chat_responder as cr

    client = HHClient()
    try:
        summary = await cr.list_reply_candidates(
            client,
            limit=max(1, limit),
            max_scan=max(1, max_scan),
        )
    finally:
        await _stop_client(client)
    print(json.dumps({"chat_candidates": summary}, ensure_ascii=False))


async def respond_one(
    chat_id: str,
    *,
    message_id: str,
    allow_suspicious: bool,
    allow_any: bool,
    force_send: bool,
) -> None:
    import hh_chat_responder as cr

    client = HHClient()
    try:
        detail = await cr.process_one(
            client,
            chat_id,
            message_id=message_id,
            allow_suspicious=allow_suspicious,
            allow_any=allow_any,
            dry_run=False if force_send else True,
            notify=True,
        )
    finally:
        await _stop_client(client)

    print("📋 Chat one-shot summary:")
    print(f"  Чат: {detail.get('chat_id', chat_id)}")
    print(f"  OK: {detail.get('ok')}")
    print(f"  Сообщение: {detail.get('message', '')}")
    if detail.get("already_replied"):
        print("  Уже отвечали на это сообщение")
    if detail.get("question"):
        print(f"  Вопрос: {detail.get('question', '')[:220]}")
    if detail.get("answer"):
        print(f"  Ответ: {detail.get('answer', '')[:400]}")
    if detail.get("dry_run"):
        print(f"  DRY-RUN скрин: {(detail.get('preview') or {}).get('screenshot_path', '-')}")
    elif detail.get("sent"):
        print("  SENT: yes")
