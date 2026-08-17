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
