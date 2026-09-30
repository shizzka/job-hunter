import asyncio

import hh_client
from hh import apply as hh_apply


def test_legacy_apply_predicates_are_reexported():
    assert hh_client._has_archived_hh_state is hh_apply.has_archived_hh_state
    assert (
        hh_client._looks_like_closed_or_archived_hh
        is hh_apply.looks_like_closed_or_archived_hh
    )
    assert (
        hh_client._looks_like_existing_hh_response
        is hh_apply.looks_like_existing_hh_response
    )
    assert hh_client._looks_like_hh_apply_success is hh_apply.looks_like_hh_apply_success


def test_closed_or_archived_predicate_ignores_generic_html_shell():
    html = "<html><template>Вакансия не принимает отклики</template></html>"

    assert hh_apply.looks_like_closed_or_archived_hh(html) is False


def test_closed_or_archived_predicate_detects_serialized_archived_state():
    html = '<script>window.data = {"archived": true}</script>'

    assert hh_apply.looks_like_closed_or_archived_hh(html) is True


def test_apply_success_includes_existing_response_state():
    assert hh_apply.looks_like_hh_apply_success("Вы уже откликнулись") is True


class FakePage:
    def __init__(self, *, url="https://hh.ru/vacancy/1", text=""):
        self.url = url
        self.text = text
        self.selectors = []

    async def evaluate(self, script):
        assert "document.body.innerText.slice" in script
        return self.text

    async def query_selector(self, selector):
        self.selectors.append(selector)
        return None


class FakeSession:
    def __init__(self, page):
        self._page = page

    async def _page_text(self, limit=12000):
        return self._page.text[:limit]


def test_page_text_reads_through_session_page():
    session = FakeSession(FakePage(text="Отклик отправлен"))

    assert asyncio.run(hh_apply.page_text(session, 100)) == "Отклик отправлен"


def test_response_requires_questions_detects_question_url_without_dom_lookup():
    page = FakePage(url="https://hh.ru/applicant/vacancy_response_question?vacancyId=1")
    session = FakeSession(page)

    assert asyncio.run(
        hh_apply.response_requires_questions(session, logger=hh_client.log)
    ) is True
    assert page.selectors == []


def test_response_requires_questions_ignores_generic_copy_for_cover_letter_only():
    page = FakePage(
        url="https://hh.ru/applicant/vacancy_response?vacancyId=1",
        text="Для отклика необходимо ответить на несколько вопросов работодателя",
    )
    session = FakeSession(page)

    assert asyncio.run(
        hh_apply.response_requires_questions(session, logger=hh_client.log)
    ) is False


def test_response_requires_questions_detects_structural_question_fields():
    page = FakePage(url="https://hh.ru/applicant/vacancy_response?vacancyId=1")
    session = FakeSession(page)

    async def inspect():
        return {"fields": [{"field_id": "salary"}], "unsupported_fields": 0}

    session._inspect_employer_questions = inspect

    assert asyncio.run(
        hh_apply.response_requires_questions(session, logger=hh_client.log)
    ) is True


class FakeResponseFormPage:
    def __init__(self, other_fields=0):
        self.url = "https://hh.ru/applicant/vacancy_response?vacancyId=1"
        self.other_fields = other_fields

    async def evaluate(self, script):
        assert "codex:response-form-signature" in script
        return {
            "controls": [
                "textarea::letter:vacancy-response-popup-form-letter-input::required",
                "button:submit::vacancy-response-submit-popup::optional",
            ],
            "letter_count": 1,
            "submit_count": 1,
            "other_field_count": self.other_fields,
        }


class FakeResponseFormSession:
    def __init__(self, other_fields=0):
        self._page = FakeResponseFormPage(other_fields)


def test_response_form_signature_classifies_cover_letter_only_form():
    session = FakeResponseFormSession(other_fields=0)

    signature = asyncio.run(hh_apply.response_form_signature(session))

    assert signature["letter_count"] == 1
    assert signature["submit_count"] == 1
    assert signature["other_field_count"] == 0
    assert len(signature["fingerprint"]) == 12
    assert asyncio.run(
        hh_apply.response_requires_questions(session, logger=hh_client.log)
    ) is False


def test_response_form_signature_classifies_real_question_fields():
    session = FakeResponseFormSession(other_fields=2)

    assert asyncio.run(
        hh_apply.response_requires_questions(session, logger=hh_client.log)
    ) is True


def test_legacy_response_questions_wrapper_forwards_patchable_dependencies(monkeypatch):
    client = hh_client.HHClient()
    captured = {}

    async def fake_requires(session, current_url, *, logger):
        captured.update(session=session, current_url=current_url, logger=logger)
        return True

    monkeypatch.setattr(hh_client, "_response_requires_questions", fake_requires)

    assert asyncio.run(client._response_requires_questions("question-url")) is True
    assert captured == {
        "session": client,
        "current_url": "question-url",
        "logger": hh_client.log,
    }


class FakeDomSubmitPage:
    def __init__(self):
        self.script = ""

    async def evaluate(self, script):
        self.script = script
        assert "form[name='vacancy_response']" in script
        assert "vacancy-response-submit-popup" in script
        assert "modal-overlay" in script
        assert "requestSubmit(button)" in script
        return True


def test_submit_response_form_via_dom_returns_page_result():
    page = FakeDomSubmitPage()
    session = FakeSession(page)

    assert asyncio.run(
        hh_apply.submit_response_form_via_dom(session, logger=hh_client.log)
    ) is True


class FakeVisibleElement:
    def __init__(self, visible=True):
        self.visible = visible

    async def is_visible(self):
        return self.visible


class FakeSubmitLookupPage:
    def __init__(self, active, background):
        self.active = active
        self.background = background
        self.selectors = []

    async def query_selector_all(self, selector):
        self.selectors.append(selector)
        if selector.startswith("[data-qa='modal-overlay']"):
            return [self.active]
        if selector == "[data-qa='vacancy-response-submit-popup']":
            return [self.background, self.active]
        return []


def test_response_submit_button_prefers_active_modal_over_background_control():
    active = FakeVisibleElement()
    background = FakeVisibleElement()
    page = FakeSubmitLookupPage(active, background)
    session = FakeSession(page)

    result = asyncio.run(hh_apply.response_submit_button(session))

    assert result is active
    assert page.selectors == [
        "[data-qa='modal-overlay'] [data-qa='vacancy-response-submit-popup']"
    ]


def test_legacy_debug_snapshot_wrapper_forwards_active_state_dir(monkeypatch):
    client = hh_client.HHClient()
    captured = {}

    async def fake_snapshot(session, prefix, *, state_dir):
        captured.update(session=session, prefix=prefix, state_dir=state_dir)

    monkeypatch.setattr(hh_client, "_save_debug_snapshot", fake_snapshot)
    monkeypatch.setattr(hh_client.config, "HH_STATE_DIR", "/tmp/hh-state")

    asyncio.run(client._save_debug_snapshot("apply"))

    assert captured == {
        "session": client,
        "prefix": "apply",
        "state_dir": "/tmp/hh-state",
    }


def test_legacy_detect_controls_wrapper_uses_instance(monkeypatch):
    client = hh_client.HHClient()
    captured = {}

    async def fake_detect(session):
        captured["session"] = session
        return ("url", None, False, None, None, None)

    monkeypatch.setattr(hh_client, "_detect_response_controls", fake_detect)

    result = asyncio.run(client._detect_response_controls())

    assert result == ("url", None, False, None, None, None)
    assert captured["session"] is client


class ExistingCoverLetterPage:
    frames = []

    async def evaluate(self, script):
        assert "document.body" in script
        return "HELLO   WORLD"

    async def query_selector(self, selector):
        raise AssertionError("already visible cover letter must not be sent again")


class ExistingCoverLetterSession:
    def __init__(self):
        self._page = ExistingCoverLetterPage()
        self.expanded = False

    async def _expand_cover_letter_input(self):
        self.expanded = True
        return True


def test_fill_cover_letter_post_apply_skips_duplicate_visible_text():
    session = ExistingCoverLetterSession()

    asyncio.run(
        hh_apply.fill_cover_letter_post_apply(
            session,
            "Hello world",
            logger=hh_client.log,
        )
    )

    assert session.expanded is False


def test_legacy_cover_letter_wrapper_forwards_patchable_dependencies(monkeypatch):
    client = hh_client.HHClient()
    captured = {}

    async def fake_fill(session, cover_letter, *, logger):
        captured.update(
            session=session,
            cover_letter=cover_letter,
            logger=logger,
        )

    monkeypatch.setattr(hh_client, "_fill_cover_letter_post_apply", fake_fill)

    asyncio.run(client._fill_cover_letter_post_apply("Cover letter"))

    assert captured == {
        "session": client,
        "cover_letter": "Cover letter",
        "logger": hh_client.log,
    }


def test_legacy_apply_wrapper_forwards_arguments_and_dependencies(monkeypatch):
    client = hh_client.HHClient()
    captured = {}

    async def fake_apply(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return {"ok": True, "message": "Отклик отправлен"}

    monkeypatch.setattr(hh_client, "_apply_to_vacancy", fake_apply)

    result = asyncio.run(
        client.apply_to_vacancy(
            "/vacancy/42",
            "Cover",
            "/applicant/vacancy_response?vacancyId=42",
            "QA Resume",
            "resume-1",
            "QA vacancy",
        )
    )

    assert result == {"ok": True, "message": "Отклик отправлен"}
    assert captured["args"] == (
        client,
        "/vacancy/42",
        "Cover",
        "/applicant/vacancy_response?vacancyId=42",
        "QA Resume",
        "resume-1",
        "QA vacancy",
    )
    assert captured["kwargs"]["absolute_hh_url"] is hh_client._absolute_hh_url
    assert captured["kwargs"]["anti_bot_message"] is hh_client._anti_bot_message
    assert captured["kwargs"]["logger"] is hh_client.log
