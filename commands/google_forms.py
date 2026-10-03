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


async def recheck(token: str, *, profile_name: str, submit_after: bool = False, approval_revision: str | None = None) -> None:
    import google_form_filler as gforms
    from google_forms import drafts
    from google_forms.workflow import FormWorkflow, answers_for
    paths = gforms._runtime_paths()
    workflow = FormWorkflow(paths.home_dir)
    item, edit_entry, captured_revision = workflow.capture(token)
    if approval_revision is not None and approval_revision != captured_revision:
        raise ValueError('Черновик изменён после подтверждения. Откройте новую версию через /forms.')
    edits = edit_entry.get('answers', {})
    approved = answers_for(item, edits)
    if submit_after and any(drafts.needs_review(q, approved.get(int(q["index"]), {})) for q in item["questions"]):
        raise ValueError("Сначала уточните поля с ⚠ в меню анкет.")
    if submit_after and approval_revision is None:
        raise ValueError('Нет актуального подтверждения версии. Откройте новую кнопку отправки через /forms.')
    client = HHClient()
    try:
        await client.start(headless=True)
        detail = await gforms.preview_form(
            client._page, item["form_url"], profile_name=profile_name,
            chat_id=item.get("chat_id", ""), message_id=item.get("message_id", ""),
            vacancy=item.get("vacancy"), source_message=item.get("source_message", ""),
            notify=False, runtime_paths=paths, saved_draft=item, manual_edits=edits, persist=False,
        )
        new_revision = None
        if detail.get("token") and (detail.get("questions") or detail.get("status") == "already_submitted"):
            new_revision = workflow.publish_recheck(token, captured_revision, detail)
            await gforms.notify_form_preview(detail, profile_name=profile_name)
        if submit_after:
            # Approval applies only to the questions and answers the user saw.
            def snapshot(questions, answers):
                return [(drafts.question_key(q), bool(q.get("required")),
                         drafts.answer_text(answers.get(int(q["index"]), {})),
                         bool(answers.get(int(q["index"]), {}).get("skip"))) for q in questions]
            actual = {int(a["index"]): a for a in detail.get("answers", [])}
            if (new_revision and detail.get("ok") and detail.get("status") == "preview"
                    and snapshot(item["questions"], approved) == snapshot(detail.get("questions", []), actual)):
                result = await gforms.submit_saved_preview(client, detail["token"], notify=True, runtime_paths=paths,
                                                          expected_revision=new_revision)
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
