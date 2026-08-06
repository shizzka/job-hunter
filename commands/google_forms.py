"""Google Form CLI command handlers."""

import sys

from hh_client import HHClient


async def _stop_client(client: HHClient) -> None:
    try:
        await client.stop()
    except Exception:
        pass


async def preview(chat_id: str, *, message_id: str, profile_name: str) -> None:
    import google_form_filler as gforms

    client = HHClient()
    try:
        detail = await gforms.preview_from_hh_chat(
            client,
            chat_id,
            message_id=message_id,
            profile_name=profile_name,
            notify=True,
        )
    finally:
        await _stop_client(client)

    print("📋 Google Form preview summary:")
    print(f"  OK: {detail.get('ok')}")
    print(f"  Сообщение: {detail.get('message', '')}")
    print(f"  Чат: {detail.get('chat_id', chat_id)}")
    print(f"  Токен: {detail.get('token', '')}")
    print(f"  Вопросов: {len(detail.get('questions') or [])}")
    filled = (detail.get("fill_result") or {}).get("filled") or []
    skipped = (detail.get("fill_result") or {}).get("skipped") or []
    print(f"  Заполнено: {len(filled)} | пропущено: {len(skipped)}")
    if not detail.get("ok"):
        sys.exit(1)


async def submit(token: str) -> None:
    import google_form_filler as gforms

    client = HHClient()
    try:
        detail = await gforms.submit_saved_preview(client, token, notify=True)
    finally:
        await _stop_client(client)

    print("📋 Google Form submit summary:")
    print(f"  OK: {detail.get('ok')}")
    print(f"  Сообщение: {detail.get('message', '')}")
    print(f"  Токен: {detail.get('token', token)}")
    if not detail.get("ok"):
        sys.exit(1)
