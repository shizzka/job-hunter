import asyncio

import config

from hh_client import (
    HHClient,
    _looks_like_closed_or_archived_hh,
    _looks_like_existing_hh_response,
    _looks_like_hh_apply_success,
    _looks_like_resume_boost_action,
    _looks_like_resume_boost_success,
    _looks_like_resume_boost_unavailable,
    _question_answer_item,
    _answer_question_from_library,
    _is_risky_question,
    _resume_matches_target,
)


class FakePage:
    def __init__(
        self,
        *,
        url: str,
        html: str,
        resume_count: int = 0,
        closed: bool = False,
        selector_hits: dict[str, object] | None = None,
    ):
        self.url = url
        self._html = html
        self._resume_count = resume_count
        self._closed = closed
        self._selector_hits = selector_hits or {}
        self.goto_calls = []

    async def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None):
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})

    async def wait_for_timeout(self, timeout_ms: int):
        return None

    async def content(self) -> str:
        return self._html

    async def query_selector_all(self, selector: str):
        return [object() for _ in range(self._resume_count)]

    async def query_selector(self, selector: str):
        return self._selector_hits.get(selector)

    def is_closed(self) -> bool:
        return self._closed


class FakeContext:
    def __init__(self, names: list[str] | None = None):
        self.names = names or []

    async def cookies(self, *args, **kwargs):
        return [{"name": name} for name in self.names]


def test_is_logged_in_false_on_login_redirect():
    client = HHClient()
    client._page = FakePage(url="https://hh.ru/account/login?backurl=%2Fapplicant%2Fresumes", html="")

    assert asyncio.run(client.is_logged_in()) is False


def test_is_logged_in_false_on_forbidden_resume_page():
    client = HHClient()
    client._page = FakePage(
        url="https://hh.ru/applicant/resumes",
        html="""
        <html>
          <body>
            <script>
              window.__data = {"userType": "anonymous", "luxPageName": "ForbiddenPage"};
            </script>
          </body>
        </html>
        """,
    )

    assert asyncio.run(client.is_logged_in()) is False


def test_is_logged_in_true_on_resume_cards():
    client = HHClient()
    client._page = FakePage(
        url="https://hh.ru/applicant/resumes",
        html="<html><body>authorized</body></html>",
        resume_count=2,
    )

    assert asyncio.run(client.is_logged_in()) is True


def test_is_logged_in_true_on_empty_authenticated_resume_page():
    client = HHClient()
    client._page = FakePage(
        url="https://hh.ru/applicant/resumes",
        html="<html><body><h1>Мои резюме</h1><div>Пока нет резюме</div></body></html>",
    )

    assert asyncio.run(client.is_logged_in()) is True


def test_is_logged_in_passive_true_on_authenticated_non_login_page():
    client = HHClient()
    client._page = FakePage(
        url="https://hh.ru/applicant/profile",
        html="<html><body>authorized</body></html>",
    )
    client._context = FakeContext(["hhtoken", "hhuid"])

    assert asyncio.run(client.is_logged_in_passive()) is True


def test_is_logged_in_passive_false_on_login_page_even_with_cookies():
    client = HHClient()
    client._page = FakePage(
        url="https://hh.ru/account/login",
        html="<html><body>login form</body></html>",
    )
    client._context = FakeContext(["hhtoken", "hhuid"])

    assert asyncio.run(client.is_logged_in_passive()) is False


def test_looks_like_existing_hh_response_detects_reapply_label():
    assert _looks_like_existing_hh_response("Отклик другим резюме") is True
    assert _looks_like_existing_hh_response("Откликнуться повторно") is True
    assert _looks_like_existing_hh_response("Вы откликнулись") is True
    assert _looks_like_existing_hh_response("Откликнуться") is False


def test_looks_like_hh_apply_success_detects_new_success_markers():
    assert _looks_like_hh_apply_success("Резюме доставлено") is True
    assert _looks_like_hh_apply_success("Отклик отправлен") is True
    assert _looks_like_hh_apply_success("Связаться с работодателем можно в чате") is True
    assert _looks_like_hh_apply_success("Откликнуться") is False


def test_looks_like_closed_or_archived_hh_detects_text_and_lux_state():
    assert _looks_like_closed_or_archived_hh("Вакансия в архиве") is True
    assert _looks_like_closed_or_archived_hh('{"analyticsParams":{"active":"false","archived":"true"}}') is True
    assert _looks_like_closed_or_archived_hh(
        '<html><template>{"translations":{"x":"Вакансия в архиве"}}</template></html>'
    ) is False
    assert _looks_like_closed_or_archived_hh("Откликнуться") is False


def test_has_existing_response_ui_uses_selector_hit():
    client = HHClient()
    client._page = FakePage(
        url="https://hh.ru/applicant/vacancy_response?vacancyId=1",
        html="<html><body>modal</body></html>",
        selector_hits={"text='Вы откликнулись'": object()},
    )

    assert asyncio.run(client._has_existing_response_ui()) is True


class FakeApplyElement:
    def __init__(self, page, *, kind: str, next_stage: str | None = None, text: str = ""):
        self.page = page
        self.kind = kind
        self.next_stage = next_stage
        self.text = text

    async def scroll_into_view_if_needed(self):
        return None

    async def click(self, timeout: int | None = None, force: bool = False):
        if self.next_stage is not None:
            self.page.stage = self.next_stage
        elif self.kind in {"apply_button", "submit_button"}:
            self.page.stage = "success"
        elif self.kind == "cover_toggle":
            self.page.letter_visible = True
        return None

    async def evaluate(self, script: str):
        return None

    async def inner_text(self):
        return self.text


class FakeTextField:
    def __init__(self):
        self.value = ""

    async def scroll_into_view_if_needed(self):
        return None

    async def click(self):
        return None

    async def fill(self, value: str):
        self.value = value

    async def type(self, value: str, delay: int = 0):
        self.value = value

    async def evaluate(self, script: str, value: str | None = None):
        if value is not None:
            self.value = value
        return None

    async def press(self, key: str):
        return None


class FakeApplyPage:
    def __init__(self):
        self.url = "https://hh.ru/vacancy/1"
        self.stage = "vacancy"
        self.frames = []

    @property
    def main_frame(self):
        return self

    async def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None):
        self.url = url

    async def wait_for_timeout(self, timeout_ms: int):
        return None

    async def screenshot(self, path: str):
        return None

    async def content(self) -> str:
        if self.stage == "success":
            return "<html><body><div>Резюме доставлено</div></body></html>"
        return "<html><body><button>Откликнуться</button></body></html>"

    async def query_selector(self, selector: str):
        if self.stage == "vacancy":
            apply_selectors = {
                "[data-qa='vacancy-response-link-top-again'], "
                "[data-qa='vacancy-response-link-bottom-again'], "
                "[data-qa='vacancy-response-link-top'], "
                "[data-qa='vacancy-response-link-bottom'], "
                "a[data-qa*='response-link'], "
                "button[data-qa*='vacancy-response']",
                "button:has-text('Откликнуться'), "
                "a:has-text('Откликнуться')",
            }
            if selector in apply_selectors:
                return FakeApplyElement(self, kind="apply_button")
            return None

        if self.stage == "success":
            success_selectors = {
                "[data-qa='vacancy-response-success-standard-notification']",
                "[data-qa*='success-standard-notification']",
                "text='Резюме доставлено'",
                "text='Отклик отправлен'",
                "text='Связаться с работодателем можно в чате'",
            }
            if selector in success_selectors:
                return object()
        return None

    async def evaluate(self, script: str, arg=None):
        if "document.body.innerText" in script:
            if self.stage == "success":
                return "Резюме доставлено\nОтклик отправлен"
            return "Откликнуться"
        if "[...document.querySelectorAll('[data-qa]')]" in script:
            return []
        return None


class FakeArchivedApplyPage(FakeApplyPage):
    async def content(self) -> str:
        return (
            '<html><body><h1>Вакансия в архиве</h1>'
            '<template>{"analyticsParams":{"active":"false","archived":"true"}}</template>'
            '</body></html>'
        )

    async def evaluate(self, script: str, arg=None):
        if "document.body.innerText" in script:
            return "Вакансия в архиве\nОтклики больше не принимаются"
        if "[...document.querySelectorAll('[data-qa]')]" in script:
            return []
        return None


class FakeDirectResponsePage:
    def __init__(self):
        self.url = "https://hh.ru/applicant/vacancy_response?vacancyId=1"
        self.stage = "response"
        self.frames = []
        self.letter = FakeTextField()
        self.letter_visible = True

    @property
    def main_frame(self):
        return self

    async def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None):
        self.url = url

    async def wait_for_timeout(self, timeout_ms: int):
        return None

    async def screenshot(self, path: str):
        return None

    async def content(self) -> str:
        if self.stage == "success":
            return "<html><body><div>Резюме доставлено</div></body></html>"
        return "<html><body><textarea></textarea><button>Откликнуться</button></body></html>"

    async def query_selector(self, selector: str):
        if self.stage == "success":
            success_selectors = {
                "[data-qa='vacancy-response-success-standard-notification']",
                "[data-qa*='success-standard-notification']",
                "text='Резюме доставлено'",
                "text='Отклик отправлен'",
                "text='Связаться с работодателем можно в чате'",
            }
            if selector in success_selectors:
                return object()
            return None

        if selector == (
            "[data-qa='vacancy-response-popup-form-letter-input'], "
            "textarea[name='letter'], "
            "textarea[data-qa*='letter'], "
            ".vacancy-response-popup textarea, "
            "textarea"
        ) and self.letter_visible:
            return self.letter

        if (
            "vacancy-response-submit-popup" in selector
            or "vacancy-response-letter-submit" in selector
            or "button[data-qa*='submit']" in selector
        ):
            return FakeApplyElement(self, kind="submit_button")

        return None

    async def query_selector_all(self, selector: str):
        return []

    async def evaluate(self, script: str, arg=None):
        if "document.body.innerText.slice(0, 2000)" in script:
            return "Форма отклика"
        if "document.body.innerText.slice(0, 4000)" in script:
            return "Форма отклика"
        if "document.body.innerText.slice(0, 12000)" in script:
            if self.stage == "success":
                return "Резюме доставлено\nОтклик отправлен"
            return "Форма отклика"
        if "document.body.innerText.slice(0, 20000)" in script:
            if self.stage == "success":
                return "Резюме доставлено\nОтклик отправлен"
            return "Форма отклика"
        if "[...document.querySelectorAll('[data-qa]')]" in script:
            return []
        return None


class FakeQuestionResponsePage(FakeDirectResponsePage):
    async def content(self) -> str:
        return (
            "<html><body>"
            "<h2>Отклик на вакансию</h2>"
            "<div>Для отклика необходимо ответить на несколько вопросов работодателя</div>"
            "<div>Ответьте на вопросы</div>"
            "<button>Откликнуться</button>"
            "</body></html>"
        )

    async def query_selector(self, selector: str):
        question_selectors = {
            "h1:has-text('Ответьте на вопросы')",
            "h2:has-text('Ответьте на вопросы')",
            "text='Ответьте на вопросы'",
            "text='Для отклика необходимо ответить на несколько вопросов работодателя'",
        }
        if selector in question_selectors:
            return object()
        return await super().query_selector(selector)

    async def evaluate(self, script: str, arg=None):
        if "document.body.innerText" in script:
            return (
                "Отклик на вакансию "
                "Для отклика необходимо ответить на несколько вопросов работодателя "
                "Ответьте на вопросы"
            )
        return await super().evaluate(script)


class FakeExpandableCoverLetterPage(FakeDirectResponsePage):
    def __init__(self):
        super().__init__()
        self.letter_visible = False

    async def content(self) -> str:
        letter = "<textarea></textarea>" if self.letter_visible else ""
        if self.stage == "success":
            return "<html><body><div>Резюме доставлено</div></body></html>"
        return (
            "<html><body>"
            "<button data-qa='add-cover-letter'>Добавить сопроводительное</button>"
            f"{letter}"
            "<button data-qa='vacancy-response-submit-popup'>Откликнуться</button>"
            "</body></html>"
        )

    async def query_selector(self, selector: str):
        if self.stage != "success" and selector in {
            "[data-qa='add-cover-letter']",
            "button[data-qa='add-cover-letter']",
            "button:has-text('Добавить сопроводительное')",
            "button:has-text('Приложить письмо')",
            "button:has-text('Добавить письмо')",
        }:
            return FakeApplyElement(self, kind="cover_toggle")
        return await super().query_selector(selector)

    async def query_selector_all(self, selector: str):
        return []

    async def evaluate(self, script: str, arg=None):
        if "document.body.innerText.slice(0, 2000)" in script:
            return "Форма отклика"
        if "document.body.innerText.slice(0, 4000)" in script:
            return "Форма отклика"
        if "document.body.innerText.slice(0, 12000)" in script:
            if self.stage == "success":
                return "Резюме доставлено\nОтклик отправлен"
            return "Форма отклика"
        if "document.body.innerText.slice(0, 20000)" in script:
            if self.stage == "success":
                return "Резюме доставлено\nОтклик отправлен"
            return "Форма отклика"
        if "[...document.querySelectorAll('[data-qa]')]" in script:
            return []
        return None


class FakeAutoAnswerQuestionPage(FakeQuestionResponsePage):
    def __init__(self, *, question_text: str, input_type: str = "text", control: str = "input"):
        super().__init__()
        self.question_text = question_text
        self.input_type = input_type
        self.control = control
        self.filled_answer = ""

    async def evaluate(self, script: str, arg=None):
        if "codex:auto-question-inspect" in script:
            return {
                "page_text": f"Ответьте на вопросы {self.question_text}",
                "fields": [
                    {
                        "field_id": "field-1",
                        "control": self.control,
                        "input_type": self.input_type,
                        "question_text": self.question_text,
                        "placeholder": "",
                        "max_length": 0,
                    }
                ],
                "unsupported_fields": 0,
            }
        if "codex:auto-question-fill" in script:
            self.filled_answer = arg[0]["answer"]
            return {"filled": len(arg), "errors": []}
        if "form.requestSubmit" in script:
            self.stage = "success"
            return True
        return await super().evaluate(script, arg=arg)


class FakeManyAutoAnswerQuestionPage(FakeAutoAnswerQuestionPage):
    def __init__(self, *, question_count: int):
        super().__init__(question_text="Вопрос 1")
        self.question_count = question_count
        self.filled_answers = []

    async def evaluate(self, script: str, arg=None):
        if "codex:auto-question-inspect" in script:
            return {
                "page_text": "Ответьте на вопросы анкеты",
                "fields": [
                    {
                        "field_id": f"field-{idx}",
                        "control": "input",
                        "input_type": "text",
                        "question_text": f"Вопрос {idx}",
                        "placeholder": "",
                        "max_length": 0,
                    }
                    for idx in range(1, self.question_count + 1)
                ],
                "unsupported_fields": 0,
            }
        if "codex:auto-question-fill" in script:
            self.filled_answers = list(arg)
            return {"filled": len(arg), "errors": []}
        return await super().evaluate(script, arg=arg)


class FakeResumeSelectionReturnsToVacancyPage:
    def __init__(self):
        self.url = "https://hh.ru/vacancy/1"
        self.stage = "vacancy"
        self.frames = []

    @property
    def main_frame(self):
        return self

    async def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None):
        self.url = url

    async def wait_for_timeout(self, timeout_ms: int):
        return None

    async def screenshot(self, path: str):
        return None

    async def content(self) -> str:
        if self.stage == "success":
            return "<html><body><div>Резюме доставлено</div></body></html>"
        if self.stage == "resume":
            return "<html><body><div>Выберите резюме</div></body></html>"
        return (
            "<html><body>"
            "<a data-qa='vacancy-response-link-top' href='/applicant/vacancy_response?vacancyId=1'>"
            "Откликнуться"
            "</a>"
            "</body></html>"
        )

    async def query_selector(self, selector: str):
        if self.stage == "success":
            success_selectors = {
                "[data-qa='vacancy-response-success-standard-notification']",
                "[data-qa*='success-standard-notification']",
                "text='Резюме доставлено'",
                "text='Отклик отправлен'",
                "text='Связаться с работодателем можно в чате'",
            }
            if selector in success_selectors:
                return object()
            return None

        if self.stage == "vacancy":
            if (
                "vacancy-response-link-top" in selector
                or "a[data-qa*='response-link']" in selector
                or "a:has-text('Откликнуться')" in selector
            ):
                return FakeApplyElement(self, kind="apply_button", next_stage="resume")
            return None

        if self.stage == "vacancy_after_resume":
            if (
                "vacancy-response-link-top" in selector
                or "a[data-qa*='response-link']" in selector
                or "a:has-text('Откликнуться')" in selector
            ):
                return FakeApplyElement(self, kind="apply_button", next_stage="success")
            return None

        return None

    async def query_selector_all(self, selector: str):
        if self.stage == "resume" and "resume" in selector:
            return [
                FakeApplyElement(
                    self,
                    kind="resume_item",
                    next_stage="vacancy_after_resume",
                    text="QA Resume",
                )
            ]
        return []

    async def evaluate(self, script: str, arg=None):
        if "document.body.innerText.slice(0, 4000)" in script:
            if self.stage == "resume":
                return "Выберите резюме QA Resume"
            if self.stage == "success":
                return "Резюме доставлено Отклик отправлен"
            return "Откликнуться"
        if "document.body.innerText.slice(0, 6000)" in script:
            if self.stage == "success":
                return "Резюме доставлено Отклик отправлен"
            return "Откликнуться"
        if "document.body.innerText.slice(0, 12000)" in script:
            if self.stage == "success":
                return "Резюме доставлено\nОтклик отправлен"
            if self.stage == "resume":
                return "Выберите резюме QA Resume"
            return "Откликнуться"
        if "document.body.innerText.slice(0, 20000)" in script:
            if self.stage == "success":
                return "Резюме доставлено\nОтклик отправлен"
            return "Откликнуться"
        if "[...document.querySelectorAll('[data-qa]')]" in script:
            return ["vacancy-response-link-top"]
        return None


def test_apply_to_vacancy_postfills_cover_letter_on_success_notification(monkeypatch):
    client = HHClient()
    client._page = FakeApplyPage()

    called = {"value": False}

    async def fake_fill_cover_letter_post_apply(cover_letter: str):
        called["value"] = True

    monkeypatch.setattr(client, "_fill_cover_letter_post_apply", fake_fill_cover_letter_post_apply)
    monkeypatch.setattr(client, "_is_captcha_page", lambda: asyncio.sleep(0, result=False))

    result = asyncio.run(
        client.apply_to_vacancy("https://hh.ru/vacancy/1", cover_letter="hello from cover letter")
    )

    assert result["ok"] is True
    assert result["message"] == "Отклик отправлен"
    assert called["value"] is True


def test_apply_to_vacancy_skips_archived_page_before_click(monkeypatch):
    client = HHClient()
    client._page = FakeArchivedApplyPage()

    monkeypatch.setattr(client, "_is_captcha_page", lambda: asyncio.sleep(0, result=False))

    result = asyncio.run(
        client.apply_to_vacancy("https://hh.ru/vacancy/1", cover_letter="hello from cover letter")
    )

    assert result["ok"] is False
    assert result["closed_or_archived"] is True
    assert result["message"] == "Вакансия закрыта или находится в архиве"
    assert client._page.stage == "vacancy"


def test_apply_to_vacancy_allows_missing_resume_picker_when_response_form_is_open(monkeypatch):
    client = HHClient()
    client._page = FakeDirectResponsePage()

    monkeypatch.setattr(client, "_is_captcha_page", lambda: asyncio.sleep(0, result=False))

    result = asyncio.run(
        client.apply_to_vacancy(
            "https://hh.ru/vacancy/1",
            cover_letter="hello from cover letter",
            preferred_resume_title="QA Resume",
        )
    )

    assert result["ok"] is True
    assert client._page.letter.value == "hello from cover letter"


def test_apply_to_vacancy_uses_link_based_apply_after_resume_selection(monkeypatch):
    client = HHClient()
    client._page = FakeResumeSelectionReturnsToVacancyPage()

    monkeypatch.setattr(client, "_is_captcha_page", lambda: asyncio.sleep(0, result=False))

    result = asyncio.run(
        client.apply_to_vacancy(
            "https://hh.ru/vacancy/1",
            preferred_resume_title="QA Resume",
        )
    )

    assert result["ok"] is True
    assert result["message"] == "Отклик отправлен"


def test_apply_to_vacancy_marks_questionnaire_as_manual(monkeypatch):
    client = HHClient()
    client._page = FakeQuestionResponsePage()

    monkeypatch.setattr(client, "_is_captcha_page", lambda: asyncio.sleep(0, result=False))

    result = asyncio.run(
        client.apply_to_vacancy(
            "https://hh.ru/vacancy/1",
            cover_letter="hello from cover letter",
        )
    )

    assert result["ok"] is False
    assert result["message"] == "Требуются доп. вопросы работодателя — пропускаем (не удалось разобрать поля формы)"


def test_apply_to_vacancy_autoanswers_salary_question(monkeypatch):
    client = HHClient()
    client._page = FakeAutoAnswerQuestionPage(question_text="Ваши зарплатные ожидания?")

    monkeypatch.setattr(client, "_is_captcha_page", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_SIMPLE_QUESTIONS", True)
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_USE_LLM", False)
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_SALARY_TEXT", "80 000 ₽ на руки")
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_SALARY_NUMBER", "80000")

    result = asyncio.run(
        client.apply_to_vacancy(
            "https://hh.ru/vacancy/1",
            cover_letter="hello from cover letter",
        )
    )

    assert result["ok"] is True
    assert client._page.filled_answer == "80 000 ₽ на руки"
    assert "notes" in result
    assert any("зарплатные ожидания" in note for note in result["notes"])


def test_apply_to_vacancy_autoanswers_resume_question_with_llm(monkeypatch):
    client = HHClient()
    client._page = FakeAutoAnswerQuestionPage(
        question_text="Какой у вас опыт API-тестирования?",
        input_type="textarea",
        control="textarea",
    )

    async def fake_llm_answer(field: dict, resume_text: str, page_text: str = "", vacancy_context: str = "") -> str | None:
        return "Есть опыт API-тестирования через Postman и проверки JSON-ответов."

    monkeypatch.setattr(client, "_is_captcha_page", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr(client, "_answer_question_with_llm", fake_llm_answer)
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_SIMPLE_QUESTIONS", True)
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_USE_LLM", True)

    result = asyncio.run(
        client.apply_to_vacancy(
            "https://hh.ru/vacancy/1",
            cover_letter="hello from cover letter",
        )
    )

    assert result["ok"] is True
    assert "API-тестирования" in client._page.filled_answer
    assert "notes" in result
    assert any("опыт api-тестирования".casefold() in note.casefold() for note in result["notes"])
    assert result["question_answers"] == [
        {
            "question": "Какой у вас опыт API-тестирования?",
            "answer": "Есть опыт API-тестирования через Postman и проверки JSON-ответов.",
            "control": "textarea",
        }
    ]


def test_apply_to_vacancy_sends_risky_question_to_manual(monkeypatch):
    client = HHClient()
    client._page = FakeAutoAnswerQuestionPage(
        question_text="Какой у вас коммерческий опыт AQA на Java/Selenium и сколько лет?",
        input_type="textarea",
        control="textarea",
    )

    monkeypatch.setattr(client, "_is_captcha_page", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_SIMPLE_QUESTIONS", True)
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_USE_LLM", True)

    result = asyncio.run(
        client.apply_to_vacancy(
            "https://hh.ru/vacancy/1",
            cover_letter="hello from cover letter",
        )
    )

    assert result["ok"] is False
    assert "ручное подтверждение рискованного вопроса" in result["message"]
    assert result["risky_question"].startswith("Какой у вас коммерческий опыт AQA")
    assert client._page.filled_answer == ""
    assert result["question_answers"] == [
        {
            "question": "Какой у вас коммерческий опыт AQA на Java/Selenium и сколько лет?",
            "answer": "Нужно ручное подтверждение: риск завысить опыт кандидата.",
            "control": "textarea",
            "skipped": True,
            "skip_reason": "risky_question",
        }
    ]


def test_apply_to_vacancy_autoanswers_fourteen_questions_when_limit_allows(monkeypatch):
    client = HHClient()
    client._page = FakeManyAutoAnswerQuestionPage(question_count=14)

    async def fake_llm_answer(field: dict, resume_text: str, page_text: str = "", vacancy_context: str = "") -> str | None:
        return f"Ответ на {field['field_id']}"

    monkeypatch.setattr(client, "_is_captcha_page", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr(client, "_answer_question_with_llm", fake_llm_answer)
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_SIMPLE_QUESTIONS", True)
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_USE_LLM", True)
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_MAX_QUESTIONS", 20)

    result = asyncio.run(
        client.apply_to_vacancy(
            "https://hh.ru/vacancy/1",
            cover_letter="hello from cover letter",
        )
    )

    assert result["ok"] is True
    assert len(client._page.filled_answers) == 14
    assert len(result["question_answers"]) == 14
    assert result["question_answers"][0]["question"] == "Вопрос 1"
    assert result["question_answers"][0]["answer"] == "Ответ на field-1"


def test_apply_to_vacancy_keeps_question_limit(monkeypatch):
    client = HHClient()
    client._page = FakeManyAutoAnswerQuestionPage(question_count=21)

    monkeypatch.setattr(client, "_is_captcha_page", lambda: asyncio.sleep(0, result=False))
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_SIMPLE_QUESTIONS", True)
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_MAX_QUESTIONS", 20)

    result = asyncio.run(
        client.apply_to_vacancy(
            "https://hh.ru/vacancy/1",
            cover_letter="hello from cover letter",
        )
    )

    assert result["ok"] is False
    assert "слишком много полей" in result["message"]
    assert result["notes"] == ["автоответ пропущен: полей 21, лимит 20"]


def test_apply_to_vacancy_expands_hidden_cover_letter_before_submit(monkeypatch):
    client = HHClient()
    client._page = FakeExpandableCoverLetterPage()

    monkeypatch.setattr(client, "_is_captcha_page", lambda: asyncio.sleep(0, result=False))

    result = asyncio.run(
        client.apply_to_vacancy(
            "https://hh.ru/vacancy/1",
            cover_letter="hello from cover letter",
        )
    )

    assert result["ok"] is True
    assert client._page.letter.value == "hello from cover letter"


def test_get_resume_ids_returns_empty_on_captcha_page():
    captcha_selector = (
        "iframe[src*='captcha'], "
        "iframe[src*='recaptcha'], "
        "iframe[src*='hcaptcha'], "
        "iframe[src*='smartcaptcha'], "
        "[class*='captcha' i], "
        "[id*='captcha' i], "
        "[data-qa='captcha']"
    )
    client = HHClient()
    client._page = FakePage(
        url="https://hh.ru/applicant/resumes",
        html="<html><body>Проверка браузера перед переходом на hh.ru</body></html>",
        selector_hits={captcha_selector: object()},
    )

    assert asyncio.run(client.get_resume_ids()) == []


def test_resume_boost_text_helpers_detect_action_and_state():
    assert _looks_like_resume_boost_action("Поднять резюме")
    assert _looks_like_resume_boost_action("Поднять")
    assert _looks_like_resume_boost_action("Обновить дату резюме")
    assert not _looks_like_resume_boost_action("Откликнуться")

    assert _looks_like_resume_boost_unavailable("Резюме можно будет поднять через 2 часа")
    assert _looks_like_resume_boost_success("Резюме поднято в поиске")


def test_resume_matches_target_by_id_title_or_url():
    resume = {
        "id": "abc123",
        "title": "QA Engineer",
        "url": "/resume/abc123?from=resume_list",
    }

    assert _resume_matches_target(resume, resume_id="abc123")
    assert _resume_matches_target(resume, resume_id="abc123", resume_title="")
    assert _resume_matches_target(resume, resume_title="QA")
    assert not _resume_matches_target(resume, resume_id="zzz", resume_title="Python developer")


def test_question_answer_item_keeps_questionnaire_metadata():
    item = _question_answer_item(
        "Есть ли опыт API?",
        "Да, REST API и Postman",
        control="textarea",
        best_guess=True,
        required=True,
        starred=True,
    )

    assert item == {
        "question": "Есть ли опыт API?",
        "answer": "Да, REST API и Postman",
        "control": "textarea",
        "best_guess": True,
        "required": True,
        "starred": True,
    }


def test_stable_answer_library_answers_api_question_without_llm():
    answer = _answer_question_from_library(
        "Какой у вас опыт API, REST, Postman и JSON?",
        max_chars=140,
    )

    assert answer is not None
    assert "REST API" in answer
    assert "Postman" in answer
    assert len(answer) <= 140


def test_stable_answer_library_skips_risky_aqa_commercial_question():
    question = "Какой у вас коммерческий опыт AQA на Java/Selenium и сколько лет?"

    assert _is_risky_question(question) is True
    assert _answer_question_from_library(question, max_chars=200) is None
