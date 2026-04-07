import asyncio

import search_pipeline


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


def test_collect_hh_vacancies_checks_next_page_even_if_current_page_is_fully_seen(monkeypatch):
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
                    for index in range(20)
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
    assert client.calls == [("qa", 0, 113, ""), ("qa", 1, 113, "")]
