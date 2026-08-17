import asyncio
from types import SimpleNamespace

from hh.resume import (
    _looks_like_resume_boost_action,
    _looks_like_resume_boost_success,
    _looks_like_resume_boost_unavailable,
    _resume_matches_target,
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
