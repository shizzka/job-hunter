import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import analytics
import search_pipeline
from hh.search import HHSearchAuthError, HHSearchStopped
from hh.ui import HHUnexpectedUI


def test_collect_all_continues_when_single_source_fails(monkeypatch):
    statuses = []
    office_events = []

    async def fake_status(action: str, msg: str, status: str) -> None:
        statuses.append((action, msg, status))

    async def fake_office_log(event: str, message: str, level: str) -> None:
        office_events.append((event, message, level))

    async def fake_collect_hh(client):
        return [{"id": "hh-1", "source": "hh"}]

    async def fake_collect_superjob(client):
        raise RuntimeError("proxy down")

    async def fake_collect_habr(client):
        return [{"id": "habr-1", "source": "habr"}]

    async def fake_collect_geekjob(client):
        return []

    monkeypatch.setattr(search_pipeline, "collect_hh_vacancies", fake_collect_hh)
    monkeypatch.setattr(search_pipeline, "collect_superjob_vacancies", fake_collect_superjob)
    monkeypatch.setattr(search_pipeline, "collect_habr_vacancies", fake_collect_habr)
    monkeypatch.setattr(search_pipeline, "collect_geekjob_vacancies", fake_collect_geekjob)
    monkeypatch.setattr(search_pipeline, "office_log", fake_office_log)
    monkeypatch.setattr(search_pipeline.config, "HH_ENABLED", True)
    monkeypatch.setattr(search_pipeline.config, "SUPERJOB_ENABLED", True)
    monkeypatch.setattr(search_pipeline.config, "HABR_ENABLED", True)
    monkeypatch.setattr(search_pipeline.config, "GEEKJOB_ENABLED", True)

    vacancies = asyncio.run(
        search_pipeline.collect_all(
            hh_client=None,
            superjob_client=None,
            habr_client=None,
            geekjob_client=None,
            status_callback=fake_status,
        )
    )

    assert [item["id"] for item in vacancies] == ["hh-1", "habr-1"]
    assert ("superjob_collect_failed", "SuperJob пропущен: proxy down", "warning") in office_events
    assert any("продолжаю без источника" in msg for _, msg, _ in statuses)


def test_collect_all_passes_source_stats_to_collectors(monkeypatch):
    source_stats = {}

    async def fake_collect_hh(client, *, scan_stats=None):
        scan_stats.setdefault(
            "hh",
            {
                "label": "hh.ru",
                "fetched": 5,
                "already_seen": 5,
                "new": 0,
                "relevant": 0,
                "applied": 0,
                "manual": 0,
                "rejected": 0,
            },
        )
        return []

    async def fake_collect_superjob(client):
        return []

    async def fake_collect_habr(client):
        return []

    async def fake_collect_geekjob(client):
        return []

    monkeypatch.setattr(search_pipeline, "collect_hh_vacancies", fake_collect_hh)
    monkeypatch.setattr(search_pipeline, "collect_superjob_vacancies", fake_collect_superjob)
    monkeypatch.setattr(search_pipeline, "collect_habr_vacancies", fake_collect_habr)
    monkeypatch.setattr(search_pipeline, "collect_geekjob_vacancies", fake_collect_geekjob)
    monkeypatch.setattr(search_pipeline.config, "HH_ENABLED", True)
    monkeypatch.setattr(search_pipeline.config, "SUPERJOB_ENABLED", False)
    monkeypatch.setattr(search_pipeline.config, "HABR_ENABLED", False)
    monkeypatch.setattr(search_pipeline.config, "GEEKJOB_ENABLED", False)

    vacancies = asyncio.run(
        search_pipeline.collect_all(
            hh_client=None,
            superjob_client=None,
            habr_client=None,
            geekjob_client=None,
            source_stats=source_stats,
        )
    )

    assert vacancies == []
    assert source_stats["hh"]["fetched"] == 5
    assert source_stats["hh"]["already_seen"] == 5


@pytest.mark.parametrize("seen_count", [1, 20])
def test_collect_hh_vacancies_checks_next_page_even_if_current_page_is_fully_seen(monkeypatch, seen_count):
    class FakeHHClient:
        def __init__(self):
            self.calls = []

        async def is_logged_in(self):
            return True

        async def search_vacancies(self, query, page=0, area=113, schedule=""):
            self.calls.append((query, page, area, schedule))
            if page == 0:
                return [
                    {
                        "id": f"seen-{index}",
                        "title": f"Seen {index}",
                        "company": "A",
                        "url": f"https://hh.ru/vacancy/{index}",
                    }
                    for index in range(seen_count)
                ]
            if page == 1:
                return [{"id": "new-2", "title": "New", "company": "B", "url": "https://hh.ru/vacancy/2"}]
            return []

        def consume_antibot_signal(self):
            return None

    async def fake_office_log(event: str, message: str, level: str) -> None:
        return None

    monkeypatch.setattr(search_pipeline.config, "HH_ENABLED", True)
    monkeypatch.setattr(search_pipeline.config, "SEARCH_PROFILES", [{"area": 113, "schedule": ""}])
    monkeypatch.setattr(search_pipeline.config, "SEARCH_QUERIES", ["qa"])
    monkeypatch.setattr(search_pipeline.config, "SEARCH_PAGES", 3)
    monkeypatch.setattr(search_pipeline.hh_guard, "can_collect", lambda: (True, ""))
    monkeypatch.setattr(search_pipeline, "office_log", fake_office_log)
    monkeypatch.setattr(
        search_pipeline.seen,
        "is_seen",
        lambda vacancy_id: vacancy_id.startswith("seen-"),
    )

    client = FakeHHClient()
    vacancies = asyncio.run(search_pipeline.collect_hh_vacancies(client))

    assert [item["id"] for item in vacancies] == ["new-2"]
    assert client.calls == [("qa", 0, 113, ""), ("qa", 1, 113, ""), ("qa", 2, 113, "")]


@pytest.fixture
def hh_collection(monkeypatch):
    monkeypatch.setattr(search_pipeline.config, "HH_ENABLED", True)
    monkeypatch.setattr(search_pipeline.config, "HH_SEARCH_REMOTE_ONLY", False)
    monkeypatch.setattr(search_pipeline.config, "SEARCH_PROFILES", [{"area": 2}, {"area": 1, "schedule": "remote"}])
    monkeypatch.setattr(search_pipeline.config, "SEARCH_QUERIES", ["manual QA", "QA engineer"])
    monkeypatch.setattr(search_pipeline.config, "SEARCH_PAGES", 3)
    monkeypatch.setattr(search_pipeline.hh_guard, "can_collect", lambda: (True, ""))
    monkeypatch.setattr(search_pipeline.seen, "is_seen", lambda vid: vid == "manual QA-2-0")
    monkeypatch.setattr(search_pipeline, "office_log", AsyncMock())
    monkeypatch.setattr(search_pipeline, "_notify_hh_auth_required_once", AsyncMock())
    return SimpleNamespace(is_logged_in=AsyncMock(return_value=True),
                           search_vacancies=AsyncMock(), consume_antibot_signal=Mock(return_value=None))


@pytest.mark.parametrize("error_type", [ValueError, TimeoutError])
def test_hh_page_failure_keeps_results_and_visits_remaining_queries_and_regions(
    hh_collection, monkeypatch, caplog, error_type,
):
    async def search(query, *, page, area, schedule):
        if (query, area, page) == ("manual QA", 2, 1):
            raise error_type("unrecognized HH search results")
        return [{"id": f"{query}-{area}-{page}"}]

    hh_collection.search_vacancies.side_effect = search
    for source in ("superjob", "habr", "geekjob"):
        monkeypatch.setattr(search_pipeline, f"collect_{source}_vacancies", AsyncMock(return_value=[]))
    stats = {}
    with analytics.observe_search("hh-page-failure", "search") as observation:
        vacancies = asyncio.run(search_pipeline.collect_all(
            hh_collection, None, None, None, source_stats=stats,
        ))
        observation.result["source_stats"] = stats
        assert observation.entry()["source_stats"]["hh"]["pages_failed"] == 1
        assert len(observation.failures) == 1
        failure = observation.failures[0]
        assert (failure["source"], failure["stage"], failure["error_kind"], failure["continued"]) == (
            "hh", "collection", error_type.__name__, True,
        )

    calls = [call.args + (call.kwargs["page"], call.kwargs["area"], call.kwargs["schedule"])
             for call in hh_collection.search_vacancies.await_args_list]
    assert calls == [(query, page, area, schedule)
                     for area, schedule in ((2, ""), (1, "remote"))
                     for query in ("manual QA", "QA engineer") for page in range(3)]
    assert [v["id"] for v in vacancies] == [f"{query}-{area}-{page}"
        for query, page, area, schedule in calls
        if (query, area, page) not in (("manual QA", 2, 0), ("manual QA", 2, 1))]
    assert all(v["source"] == "hh" and v["apply_mode"] == "auto" for v in vacancies)
    assert stats["hh"]["fetched"] == 11
    assert stats["hh"]["already_seen"] == 1
    assert "stop_reason" not in stats["hh"]
    assert "query='manual QA' [area=2] page=1" in caplog.text
    assert f"error_kind={error_type.__name__} continued=true" in caplog.text


def test_hh_empty_page_finishes_only_current_query_and_region(hh_collection):
    hh_collection.search_vacancies.return_value = []
    stats = {}
    assert asyncio.run(search_pipeline.collect_hh_vacancies(hh_collection, scan_stats=stats)) == []
    assert [(call.args[0], call.kwargs["area"], call.kwargs["page"])
            for call in hh_collection.search_vacancies.await_args_list] == [
        (query, area, 0) for area in (2, 1) for query in ("manual QA", "QA engineer")
    ]
    assert stats["hh"]["fetched"] == 0
    assert stats["hh"].get("pages_failed", 0) == 0


def test_hh_unexpected_ui_stops_before_next_page(hh_collection):
    blocked = HHUnexpectedUI("search", "synthetic-fingerprint")
    hh_collection.search_vacancies.side_effect = [[{"id": "first"}], blocked]
    with pytest.raises(HHUnexpectedUI) as raised:
        asyncio.run(search_pipeline.collect_hh_vacancies(hh_collection))
    assert raised.value is blocked
    assert hh_collection.search_vacancies.await_count == 2


def test_hh_missing_auth_stops_before_collection(hh_collection):
    hh_collection.is_logged_in.return_value = False
    stats = {}
    assert asyncio.run(search_pipeline.collect_hh_vacancies(hh_collection, scan_stats=stats)) == []
    hh_collection.search_vacancies.assert_not_awaited()
    search_pipeline._notify_hh_auth_required_once.assert_awaited_once()
    assert stats["hh"]["stop_reason"]


@pytest.mark.parametrize("error_type", [HHSearchAuthError, HHSearchStopped])
def test_hh_access_loss_stops_source_and_preserves_collected_results(hh_collection, error_type):
    hh_collection.search_vacancies.side_effect = [[{"id": "first"}], error_type("access unavailable")]
    stats = {}
    values = asyncio.run(search_pipeline.collect_hh_vacancies(hh_collection, scan_stats=stats))
    assert [v["id"] for v in values] == ["first"]
    assert hh_collection.search_vacancies.await_count == 2
    assert stats["hh"]["fetched"] == 1
    assert stats["hh"]["stop_reason"]
    assert stats["hh"].get("pages_failed", 0) == 0
    assert search_pipeline._notify_hh_auth_required_once.await_count == (error_type is HHSearchAuthError)


@pytest.mark.parametrize("raises", [False, True])
def test_hh_antibot_stops_even_if_search_page_raises(hh_collection, monkeypatch, raises):
    signal = {"kind": "captcha", "message": "challenge", "stage": "search"}
    hh_collection.consume_antibot_signal.side_effect = [None, signal]
    hh_collection.search_vacancies.side_effect = [[{"id": "first"}],
        ValueError("challenge") if raises else []]
    record = Mock(return_value={})
    monkeypatch.setattr(search_pipeline.hh_guard, "record_antibot", record)
    monkeypatch.setattr(search_pipeline.hh_guard, "format_block_note", lambda status: "captcha stop")
    stats = {}
    values = asyncio.run(search_pipeline.collect_hh_vacancies(hh_collection, scan_stats=stats))
    assert [v["id"] for v in values] == ["first"]
    assert hh_collection.search_vacancies.await_count == 2
    assert stats["hh"]["stop_reason"] == "captcha stop"
    assert stats["hh"]["fetched"] == 1
    record.assert_called_once_with(kind="captcha", raw_message="challenge", stage="search")
