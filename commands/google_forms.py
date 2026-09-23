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


async def recheck(token: str, *, profile_name: str, submit_after: bool = False) -> None:
    import google_form_filler as gforms
    from google_forms import drafts
    paths = gforms._runtime_paths()
    item = drafts.get_draft(paths.home_dir, token)
    edits = drafts.manual_answers(paths.home_dir, token)
    approved = drafts.displayed_answers(paths.home_dir, item)
    if submit_after and any(drafts.needs_review(q, approved.get(int(q["index"]), {})) for q in item["questions"]):
        raise ValueError("Сначала уточните поля с ⚠ в меню анкет.")
    client = HHClient()
    try:
        await client.start(headless=True)
        detail = await gforms.preview_form(
            client._page, item["form_url"], profile_name=profile_name,
            chat_id=item.get("chat_id", ""), message_id=item.get("message_id", ""),
            vacancy=item.get("vacancy"), source_message=item.get("source_message", ""),
            notify=True, runtime_paths=paths, saved_draft=item, manual_edits=edits,
        )
        if detail.get("token") and (detail.get("questions") or detail.get("status") == "already_submitted"):
            drafts.supersede(paths.home_dir, token, detail["token"])
        if submit_after:
            # Approval applies only to the questions and answers the user saw.
            def snapshot(questions, answers):
                return [(drafts.question_key(q), bool(q.get("required")),
                         drafts.answer_text(answers.get(int(q["index"]), {})),
                         bool(answers.get(int(q["index"]), {}).get("skip"))) for q in questions]
            actual = {int(a["index"]): a for a in detail.get("answers", [])}
            if (detail.get("ok") and detail.get("status") == "preview"
                    and snapshot(item["questions"], approved) == snapshot(detail.get("questions", []), actual)):
                result = await gforms.submit_saved_preview(client, detail["token"], notify=True, runtime_paths=paths)
                print(f"Отправка: {result.get('message', '')}")
                if not result.get("ok"):
                    sys.exit(1)
            else:
                print("Анкета не отправлена: проверьте новую версию и поля с ⚠ в меню анкет.")
    finally:
        await _stop_client(client)
    print(f"Черновик проверен: {detail.get('status')}. Заполнено: {len(detail.get('fill_result', {}).get('filled', []))}.")
    if not detail.get("ok"):
        print("Остались поля для уточнения. Откройте новый черновик в /forms.")
