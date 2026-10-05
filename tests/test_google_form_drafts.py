import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import google_form_filler as gforms
from google_forms import drafts
from runtime_context import RuntimePaths
from state_store.google_forms import GoogleFormStateRepository
from state_store.json_store import JsonStore
from telegram_app.forms import TelegramFormEditor

TOKEN = "abcdef123456"


@pytest.fixture
def stored(tmp_path):
    home = str(tmp_path)
    item = {
        "token": TOKEN, "status": "preview", "ok": True, "profile_name": "qa",
        "form_url": "https://docs.google.com/forms/d/e/example/viewform",
        "questions": [
            {"index": 0, "question": "Дата рождения", "type": "text", "required": True},
            {"index": 1, "question": "Инструменты", "type": "checkbox", "options": ["Claude", "Gemini"], "required": True},
            {"index": 2, "question": "Комментарий", "type": "text", "required": False},
        ],
        "answers": [{"index": 0, "answer": "неизвестно", "confidence": "low"},
                    {"index": 1, "options": ["Claude"], "confidence": "high"},
                    {"index": 2, "answer": "", "skip": True, "confidence": "low"}],
        "fill_result": {"filled": [{"index": 1}], "skipped": [{"index": 0}, {"index": 2}]},
        "pages_total": 1,
    }
    GoogleFormStateRepository(home).remember(TOKEN, item, trim_expired=False)
    return home, item


def test_edits_are_durable_and_do_not_falsify_browser_readiness(stored):
    home, item = stored
    drafts.save_answer(home, TOKEN, 0, "29.04.1989", 7)
    assert drafts.displayed_answers(home, item)[0]["answer"] == "29.04.1989"
    assert drafts.get_draft(home, TOKEN)["answers"] == item["answers"]
    assert drafts.manual_answers(home, TOKEN)
    # A button on an old Telegram message must not submit edited answers.
    result = asyncio.run(gforms.submit_saved_preview(
        SimpleNamespace(_page=None), TOKEN, runtime_paths=RuntimePaths(home, home, home)))
    assert result["ok"] is False
    assert "Черновик изменён" in result["message"]


def test_options_required_skip_and_terminal_state(stored):
    home, item = stored
    answer = drafts.save_answer(home, TOKEN, 1, "2, 1", 7)
    assert answer["options"] == ["Gemini", "Claude"]
    with pytest.raises(ValueError):
        drafts.save_answer(home, TOKEN, 1, "9", 7)
    with pytest.raises(ValueError):
        drafts.save_answer(home, TOKEN, 0, "/skip", 7)
    assert drafts.save_answer(home, TOKEN, 2, "/skip", 7)["skip"]
    item["status"] = "submitted"
    GoogleFormStateRepository(home).remember(TOKEN, item, trim_expired=False)
    with pytest.raises(ValueError):
        drafts.save_answer(home, TOKEN, 0, "changed", 7)


def test_replay_does_not_move_manual_answer_to_changed_question(stored):
    home, item = stored
    drafts.save_answer(home, TOKEN, 0, "29.04.1989", 7)
    answers, missing = drafts.replay_answers(
        [{**item["questions"][0], "question": "Ваш телефон"}], item, drafts.manual_answers(home, TOKEN))
    assert answers == []
    assert len(missing) == 1


class FakeBot(TelegramFormEditor):
    def __init__(self, home):
        self.home = home
        self.sent = []

    def _profile_names(self):
        return ["qa"]

    def _profile(self, name):
        return SimpleNamespace(home_dir=self.home)

    def _load_state(self):
        return JsonStore(self.home + "/test_bot_state.json").load()

    def _save_state(self, state):
        JsonStore(self.home + "/test_bot_state.json").save(state)

    def _update_state(self, mutate):
        return JsonStore(self.home + "/test_bot_state.json").update(mutate)

    def _has_active_command(self, profile):
        return False

    async def _send_text(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return {"message_id": len(self.sent) + 100}


def test_reply_binding_survives_restart_and_ignores_unrelated_messages(stored):
    home, item = stored
    principal = {"user_id": 7, "role": "admin"}
    bot = FakeBot(home)
    asyncio.run(bot._ask_form_field(7, principal, "qa", TOKEN, 0))
    restarted = FakeBot(home)
    assert not asyncio.run(restarted._accept_form_answer(7, principal, {"text": "12345"}))
    assert not asyncio.run(restarted._accept_form_answer(7, principal, {
        "text": "12345", "reply_to_message": {"message_id": 999}}))
    assert asyncio.run(restarted._accept_form_answer(7, principal, {
        "text": "29.04.1989", "reply_to_message": {"message_id": 101}}))
    assert drafts.displayed_answers(home, item)[0]["answer"] == "29.04.1989"
    assert restarted._load_state()["form_pending"] == {}
    assert "Черновик анкеты" in restarted.sent[-1][1]
    rows = restarted.sent[-1][2]["reply_markup"]["inline_keyboard"]
    assert len(rows[0]) == 3
    assert any(":send:" in b.get("callback_data", "") for row in rows for b in row)


def test_list_and_controls_hide_submit_until_rechecked(stored):
    home, item = stored
    bot = FakeBot(home)
    principal = {"user_id": 7, "role": "admin"}
    drafts.save_answer(home, TOKEN, 0, "29.04.1989", 7)
    asyncio.run(bot._show_form(7, principal, "qa", TOKEN))
    buttons = [b for row in bot.sent[-1][2]["reply_markup"]["inline_keyboard"] for b in row]
    assert any(":check:" in b.get("callback_data", "") for b in buttons)
    assert not any(b.get("callback_data", "").startswith("gform_submit:") for b in buttons)
    assert all(len(b.get("callback_data", "").encode()) <= 64 for b in buttons)
    asyncio.run(bot._list_forms(7, principal, "qa"))
    assert bot.sent[-1][2]["reply_markup"]["inline_keyboard"]


@pytest.mark.parametrize("change", [None, "question", "answer", "failure"])
def test_send_rechecks_latest_answers_and_blocks_changes(stored, monkeypatch, change):
    from commands import google_forms as commands
    from copy import deepcopy
    home, item = stored
    drafts.save_answer(home, TOKEN, 0, "29.04.1989", 7)
    drafts.save_answer(home, TOKEN, 2, "/skip", 7)
    ready = deepcopy(item)
    ready.update(token="123456abcdef", answers=list(drafts.displayed_answers(home, item).values()))
    if change == "question":
        ready["questions"][0]["question"] = "Изменённый вопрос"
    if change == "answer":
        ready["answers"][0]["answer"] = "Другой ответ"
    if change == "failure":
        ready.update(ok=False, status="needs_input")
    calls = []
    class Client:
        _page = object()
        async def start(self, **kwargs):
            pass
        async def stop(self):
            calls.append("stopped")
    async def preview(page, url, **kwargs):
        assert kwargs["manual_edits"]
        calls.append("checked")
        return ready
    async def submit(client, token, **kwargs):
        assert token == ready["token"]
        calls.append("sent")
        return {"ok": True}
    monkeypatch.setattr(commands, "HHClient", Client)
    monkeypatch.setattr(gforms, "_runtime_paths", lambda: RuntimePaths(home, home, home))
    monkeypatch.setattr(gforms, "preview_form", preview)
    monkeypatch.setattr(gforms, "notify_form_preview", AsyncMock(return_value=True))
    monkeypatch.setattr(gforms, "submit_saved_preview", submit)
    from google_forms.workflow import FormWorkflow
    _, _, revision = FormWorkflow(home).capture(TOKEN)
    asyncio.run(commands.recheck(TOKEN, profile_name="qa", submit_after=True, approval_revision=revision))
    assert calls == (["checked", "sent", "stopped"] if change is None else ["checked", "stopped"])


def test_send_with_unresolved_questions_does_not_start_browser(stored, monkeypatch):
    from commands import google_forms as commands
    home, _ = stored
    monkeypatch.setattr(gforms, "_runtime_paths", lambda: RuntimePaths(home, home, home))
    def forbidden():
        raise AssertionError("Unresolved draft must not open the browser")
    monkeypatch.setattr(commands, "HHClient", forbidden)
    with pytest.raises(ValueError, match="уточните"):
        asyncio.run(commands.recheck(TOKEN, profile_name="qa", submit_after=True))


def browser_stubs(monkeypatch, questions):
    class FakePage:
        url = ""
        async def goto(self, url, **kwargs):
            self.url = url
        async def wait_for_timeout(self, value):
            pass
        def locator(self, value):
            return self
        async def inner_text(self, **kwargs):
            return "Google Form"
    async def extract(page):
        return questions
    async def no(*args, **kwargs):
        return False
    async def yes(*args, **kwargs):
        return True
    async def shot(*args, **kwargs):
        pass
    monkeypatch.setattr(gforms, "extract_form_questions", extract)
    monkeypatch.setattr(gforms, "_fill_google_form_email_consent", no)
    monkeypatch.setattr(gforms, "_has_google_form_submit_button", yes)
    monkeypatch.setattr(gforms, "_safe_screenshot", shot)
    monkeypatch.setattr(gforms, "_new_token", lambda *args: "123456abcdef")
    return FakePage()


def test_recheck_keeps_manual_answers_and_never_clicks_submit(stored, monkeypatch):
    home, item = stored
    drafts.save_answer(home, TOKEN, 0, "29.04.1989", 7)
    drafts.save_answer(home, TOKEN, 2, "/skip", 7)
    page = browser_stubs(monkeypatch, item["questions"])
    captured = []
    async def fill(page, qs, answers, **kwargs):
        captured.extend(answers)
        return {"filled": [{"index": a["index"]} for a in answers if not a.get("skip")],
                "skipped": [{"index": a["index"]} for a in answers if a.get("skip")]}
    async def forbidden(*args, **kwargs):
        raise AssertionError("Recheck must reuse answers and must not submit")
    monkeypatch.setattr(gforms, "generate_form_answers", forbidden)
    monkeypatch.setattr(gforms, "_click_google_form_submit", forbidden)
    monkeypatch.setattr(gforms, "fill_form", fill)
    result = asyncio.run(gforms.preview_form(
        page, item["form_url"], profile_name="qa", saved_draft=item,
        manual_edits=drafts.manual_answers(home, TOKEN), runtime_paths=RuntimePaths(home, home, home)))
    assert result["ok"]
    assert result["token"] != TOKEN
    assert captured[0]["answer"] == "29.04.1989"
    assert captured[0]["source"] == "telegram_manual"
    assert captured[2]["skip"]
    assert not drafts.manual_answers(home, result["token"])


def test_low_confidence_answer_not_filled_and_asks_user(stored, monkeypatch):
    home, item = stored
    page = browser_stubs(monkeypatch, item["questions"][:1])
    async def generate(*args, **kwargs):
        return [{"index": 0, "answer": "01.01.1990", "confidence": "low"}]
    async def fill(page, qs, answers, **kwargs):
        assert answers[0]["skip"]
        return {"filled": [], "skipped": [{"index": 0}]}
    monkeypatch.setattr(gforms, "generate_form_answers", generate)
    monkeypatch.setattr(gforms, "fill_form", fill)
    result = asyncio.run(gforms.preview_form(page, item["form_url"], profile_name="qa",
                                            runtime_paths=RuntimePaths(home, home, home)))
    assert result["status"] == "needs_input"
    assert result["review_indices"] == [0]


def test_changed_form_blocks_submit(stored, monkeypatch):
    home, item = stored
    item["answers"] = [{"index": q["index"], "answer": "known", "confidence": "high"} for q in item["questions"]]
    GoogleFormStateRepository(home).remember(TOKEN, item, trim_expired=False)
    page = browser_stubs(monkeypatch, [{**item["questions"][0], "question": "Новый вопрос"}])
    async def forbidden(*args, **kwargs):
        raise AssertionError("Changed form must not be filled or submitted")
    monkeypatch.setattr(gforms, "fill_form", forbidden)
    monkeypatch.setattr(gforms, "_click_google_form_submit", forbidden)
    result = asyncio.run(gforms.submit_saved_preview(SimpleNamespace(_page=page), TOKEN,
                                                    runtime_paths=RuntimePaths(home, home, home)))
    assert not result["submitted"]
    assert "изменились" in result["message"]


def test_superseded_draft_cannot_be_edited_or_submitted(stored):
    home, item = stored
    drafts.supersede(home, TOKEN, "123456abcdef")
    with pytest.raises(ValueError, match="новая"):
        drafts.save_answer(home, TOKEN, 0, "new", 7)
    result = asyncio.run(gforms.submit_saved_preview(SimpleNamespace(_page=None), TOKEN,
                                                    runtime_paths=RuntimePaths(home, home, home)))
    assert not result["ok"]


def test_personal_facts_are_exact_and_phone_model_is_not_contact(monkeypatch):
    import facts
    from google_forms import answering
    monkeypatch.setattr(facts, "load_facts", lambda: {"confirmed": {"date_of_birth": "29.04.1989", "phone_model": "Mi 13T Pro"}})
    monkeypatch.setenv("CANDIDATE_PHONE", "+70000000000")
    qs = [{"index": 0, "type": "text", "question": "Дата рождения"},
          {"index": 1, "type": "text", "question": "Марка и модель телефона"}]
    answers = answering._prepare_form_answers(qs, [])
    assert [a["answer"] for a in answers] == ["29.04.1989", "Mi 13T Pro"]


def test_multistep_recheck_discovers_new_fields_without_overwriting_manual_answers(stored, monkeypatch):
    home, item = stored
    item["questions"] = [{"index": 0, "page_index": 0, "page_question_index": 0,
                          "question": "Дата рождения", "type": "text", "required": True}]
    item["answers"] = [{"index": 0, "answer": "", "confidence": "low"}]
    GoogleFormStateRepository(home).remember(TOKEN, item, trim_expired=False)
    drafts.save_answer(home, TOKEN, 0, "29.04.1989", 7)
    page = browser_stubs(monkeypatch, [])
    page.step = 0
    generated = []
    async def extract(page):
        return [{"index": 0, "question": "Дата рождения" if page.step == 0 else "Новый вопрос",
                 "type": "text", "required": True}]
    async def next_page(page):
        page.step += 1
        return True, ""
    async def has_submit(page):
        return page.step == 1
    async def generate(qs, **kwargs):
        generated.extend(qs)
        return [{"index": 1, "answer": "", "skip": True, "confidence": "low"}]
    async def fill(page, qs, answers, **kwargs):
        if page.step == 0:
            assert answers[0]["answer"] == "29.04.1989"
        return {"filled": [{"index": a["index"]} for a in answers if not a.get("skip")],
                "skipped": [{"index": a["index"]} for a in answers if a.get("skip")]}
    async def forbidden(*args, **kwargs):
        raise AssertionError("A preview must never submit")
    monkeypatch.setattr(gforms, "extract_form_questions", extract)
    monkeypatch.setattr(gforms, "_click_google_form_next", next_page)
    monkeypatch.setattr(gforms, "_has_google_form_submit_button", has_submit)
    monkeypatch.setattr(gforms, "generate_form_answers", generate)
    monkeypatch.setattr(gforms, "fill_form", fill)
    monkeypatch.setattr(gforms, "_click_google_form_submit", forbidden)
    paths = RuntimePaths(home, home, home)
    first = asyncio.run(gforms.preview_form(page, item["form_url"], profile_name="qa",
                        saved_draft=item, manual_edits=drafts.manual_answers(home, TOKEN), runtime_paths=paths))
    assert len(first["questions"]) == 2
    assert first["status"] == "needs_input"
    assert [q["question"] for q in generated] == ["Новый вопрос"]
    drafts.save_answer(home, first["token"], 1, "Ответ со смартфона", 7)
    page.step = 0
    monkeypatch.setattr(gforms, "generate_form_answers", forbidden)
    monkeypatch.setattr(gforms, "_new_token", lambda *args: "112233aabbcc")
    final = asyncio.run(gforms.preview_form(page, item["form_url"], profile_name="qa",
                        saved_draft=first, manual_edits=drafts.manual_answers(home, first["token"]), runtime_paths=paths))
    assert final["ok"] is True
    assert [a["answer"] for a in final["answers"]] == ["29.04.1989", "Ответ со смартфона"]


def test_partial_preview_notification_has_editor_and_fits_telegram(stored, monkeypatch, tmp_path):
    import notifier
    home, item = stored
    photo = tmp_path / "shot.png"
    photo.touch()
    item.update(ok=False, screenshot_path=str(photo), message="Нужно уточнить",
                answers=[{"index": q["index"], "confidence": "low"} for q in item["questions"]])
    item["questions"][0]["question"] = "<&" * 500
    calls = []
    async def send(*args, **kwargs):
        calls.append((args, kwargs))
        return True
    monkeypatch.setattr(notifier, "send_photo", send)
    monkeypatch.setattr(notifier, "send_message_with_markup", send)
    assert asyncio.run(gforms.notify_form_preview(item, profile_name="qa"))
    buttons = [b for row in calls[-1][1]["reply_markup"]["inline_keyboard"] for b in row]
    assert any(b.get("callback_data", "").startswith("gf:view:") for b in buttons)
    assert not any(b.get("callback_data", "").startswith("gform_submit:") for b in buttons)
    for args, kw in calls:
        assert len(kw["caption"]) <= 1024 if "caption" in kw else len(args[0]) <= 4096


def test_form_callback_rejects_non_admin_before_access(monkeypatch):
    from telegram_app.callbacks import TelegramCallbackRouter
    import telegram_access
    calls = []
    class Router(TelegramCallbackRouter):
        async def _answer_callback_query(self, *args, **kwargs):
            calls.append(args)
        async def _form_callback(self, *args):
            raise AssertionError("Unauthorized callback reached editor")
    monkeypatch.setattr(telegram_access, "resolve_user", lambda uid: {"role": "user", "user_id": uid})
    asyncio.run(Router()._handle_callback_query({"id": "cb", "from": {"id": 9},
                "data": f"gf:view:qa:{TOKEN}:0", "message": {"chat": {"id": 9, "type": "private"}}}))
    assert "администратора" in calls[0][1]
