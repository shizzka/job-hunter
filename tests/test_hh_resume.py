import asyncio
from types import SimpleNamespace

from hh.resume import (
    _element_is_disabled,
    _element_text_summary,
    _find_resume_boost_action,
    _find_resume_boost_scope,
    _inspect_resume_boost,
    _looks_like_resume_boost_action,
    _looks_like_resume_boost_success,
    _looks_like_resume_boost_unavailable,
    _resume_matches_target,
    boost_resume,
    get_resume_boost_status,
    get_resume_ids,
)


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


class FakeResumeLink:
    def __init__(self, href: str, title: str):
        self.href = href
        self.title = title

    async def get_attribute(self, name: str):
        assert name == "href"
        return self.href

    async def inner_text(self):
        return self.title


class FakeResumeListPage:
    def __init__(self, links):
        self.url = "about:blank"
        self.links = links
        self.goto_calls = []

    async def goto(self, url: str, **kwargs):
        self.url = url
        self.goto_calls.append((url, kwargs))

    async def wait_for_timeout(self, timeout_ms: int):
        return None

    async def screenshot(self, **kwargs):
        return None

    async def content(self):
        return "<html></html>"

    async def query_selector_all(self, selector: str):
        if selector == "[data-qa='resume'], [data-qa^='resume-card-link-']":
            return []
        assert selector == "a[href*='/resume/']"
        return self.links


class FakeResumeListSession:
    def __init__(self, page):
        self._page = page
        self.dismissed_modal = False

    async def _dismiss_whats_new_modal(self):
        self.dismissed_modal = True

    async def _detect_anti_bot_kind(self):
        return None

    def _remember_antibot_signal(self, *args):
        raise AssertionError("unexpected anti-bot signal")


def test_get_resume_ids_uses_link_fallback_and_deduplicates(tmp_path):
    page = FakeResumeListPage(
        [
            FakeResumeLink("/resume/abc123?from=list", "QA Engineer"),
            FakeResumeLink("/resume/abc123?from=duplicate", "Duplicate"),
            FakeResumeLink("/not-a-resume", "Ignore"),
        ]
    )
    session = FakeResumeListSession(page)
    settings = SimpleNamespace(
        HH_BASE_URL="https://spb.hh.ru",
        HH_STATE_DIR=str(tmp_path),
    )

    result = asyncio.run(
        get_resume_ids(
            session,
            anti_bot_message=lambda kind, suffix: f"{kind}: {suffix}",
            settings=settings,
        )
    )

    assert result == [
        {
            "id": "abc123",
            "title": "QA Engineer",
            "url": "/resume/abc123?from=list",
        }
    ]
    assert page.goto_calls == [
        (
            "https://spb.hh.ru/applicant/resumes",
            {"wait_until": "domcontentloaded", "timeout": 30000},
        )
    ]
    assert session.dismissed_modal is True


class FakeBoostElement:
    def __init__(self, text: str, attributes: dict[str, str | None] | None = None):
        self.text = text
        self.attributes = attributes or {}

    async def inner_text(self):
        return self.text

    async def get_attribute(self, name: str):
        return self.attributes.get(name)


class FakeBoostCard:
    def __init__(self, text: str, href: str, elements=()):
        self.text = text
        self.href = href
        self.elements = list(elements)

    async def inner_text(self):
        return self.text

    async def query_selector_all(self, selector: str):
        if selector == "a[href*='/resume/']":
            return [FakeResumeLink(self.href, self.text)]
        assert selector == "button, a, [role='button']"
        return self.elements


class FakeBoostPage:
    def __init__(self, cards=(), elements=()):
        self.url = "https://spb.hh.ru/applicant/resumes"
        self.cards = list(cards)
        self.elements = list(elements)
        self.wait_calls = []

    async def query_selector_all(self, selector: str):
        if selector == "[data-qa='resume'], [data-qa^='resume-card'], [data-qa*='resume-card']":
            return self.cards
        assert selector == "button, a, [role='button']"
        return self.elements

    async def wait_for_timeout(self, timeout_ms: int):
        self.wait_calls.append(timeout_ms)


def test_resume_boost_element_helpers_preserve_text_and_disabled_state():
    element = FakeBoostElement(
        "  Поднять\nрезюме  ",
        {
            "aria-label": "Boost",
            "data-qa": "resume-boost",
            "disabled": "",
            "aria-disabled": "false",
        },
    )

    assert asyncio.run(_element_text_summary(element)) == "Поднять резюме Boost resume-boost"
    assert asyncio.run(_element_is_disabled(element)) is True


def test_find_resume_boost_scope_matches_resume_id():
    card = FakeBoostCard("QA Engineer", "/resume/abc123?from=list")
    session = SimpleNamespace(_page=FakeBoostPage(cards=[card]))

    result = asyncio.run(_find_resume_boost_scope(session, resume_id="abc123"))

    assert result is card


class FakeBoostActionSession:
    def __init__(self, page, scope):
        self._page = page
        self.scope = scope

    async def _find_resume_boost_scope(self, resume_id: str, resume_title: str):
        return self.scope

    async def _element_text_summary(self, element):
        return await _element_text_summary(element)

    async def _element_is_disabled(self, element):
        return await _element_is_disabled(element)


def test_find_resume_boost_action_prefers_matching_card():
    ignored = FakeBoostElement("Откликнуться")
    action = FakeBoostElement("Поднять резюме", {"aria-disabled": "false"})
    scope = FakeBoostCard("QA Engineer", "/resume/abc123", [ignored, action])
    session = FakeBoostActionSession(FakeBoostPage(), scope)

    result = asyncio.run(_find_resume_boost_action(session, "abc123", "QA Engineer"))

    assert result == {
        "element": action,
        "button_text": "Поднять резюме",
        "disabled": False,
    }


class FakeInspectSession:
    def __init__(self, element):
        self._page = FakeBoostPage()
        self.element = element

    async def get_resume_ids(self):
        return [
            {"id": "abc123", "title": "QA Engineer", "url": "/resume/abc123"},
            {"id": "other", "title": "Other", "url": "/resume/other"},
        ]

    async def _page_text(self, limit: int):
        assert limit == 20000
        return "Мои резюме"

    async def _save_resume_boost_debug(self, stage: str):
        return {"debug_stage": stage}

    async def _find_resume_boost_action(self, resume_id: str, resume_title: str):
        assert (resume_id, resume_title) == ("abc123", "QA Engineer")
        return {
            "element": self.element,
            "button_text": "Поднять резюме",
            "disabled": False,
        }

    async def _inspect_resume_boost(self, resume_id: str = "", resume_title: str = ""):
        return await _inspect_resume_boost(self, resume_id, resume_title)


def test_inspect_and_status_keep_targeting_but_hide_internal_element():
    element = object()
    session = FakeInspectSession(element)

    detail = asyncio.run(_inspect_resume_boost(session, resume_id="abc123"))
    status = asyncio.run(get_resume_boost_status(session, resume_id="abc123"))

    assert detail["reason"] == "boost_action_available"
    assert detail["can_boost"] is True
    assert detail["_element"] is element
    assert detail["resumes_found"] == 2
    assert status["reason"] == "boost_action_available"
    assert "_element" not in status


class FakeBoostRunSession:
    def __init__(self, element):
        self._page = FakeBoostPage()
        self.element = element
        self.click_calls = []

    async def _inspect_resume_boost(self, **kwargs):
        return {
            "ok": True,
            "can_boost": True,
            "reason": "boost_action_available",
            "resumes_found": 1,
            "_element": self.element,
        }

    async def _click_with_fallbacks(self, element, label: str):
        self.click_calls.append((element, label))
        return True

    async def _page_text(self, limit: int):
        assert limit == 20000
        return "Резюме поднято в поиске"

    async def _save_resume_boost_debug(self, stage: str):
        return {"debug_stage": stage}


def test_boost_resume_preserves_confirmation_click_and_success_state():
    element = object()
    session = FakeBoostRunSession(element)
    settings = SimpleNamespace(
        HH_RESUME_BOOST_ENABLED=True,
        HH_RESUME_BOOST_CONFIRM_TEXT="ПОДНЯТЬ",
    )

    result = asyncio.run(
        boost_resume(
            session,
            resume_id="abc123",
            confirm="ПОДНЯТЬ",
            settings=settings,
        )
    )

    assert result["reason"] == "boost_success"
    assert result["ok"] is True
    assert result["can_boost"] is False
    assert result["debug_stage"] == "after_click"
    assert session.click_calls == [(element, "resume_boost")]
    assert session._page.wait_calls == [2500]
