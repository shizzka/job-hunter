import asyncio

import agent
import config


def test_do_fresh_search_uses_lightweight_hh_only_overrides(monkeypatch):
    captured = {}
    monkeypatch.setattr(config, "HH_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HH_FRESH_SEARCH_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HH_FRESH_SEARCH_QUERIES", ["manual QA", "QA тестировщик"], raising=False)
    monkeypatch.setattr(config, "HH_FRESH_SEARCH_PAGES", 1, raising=False)
    monkeypatch.setattr(config, "HH_FRESH_SEARCH_INTERVAL_MIN", 120, raising=False)
    monkeypatch.setattr(config, "SEARCH_QUERIES", ["wide query"], raising=False)
    monkeypatch.setattr(config, "SEARCH_PAGES", 3, raising=False)
    monkeypatch.setattr(config, "SUPERJOB_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HABR_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "GEEKJOB_ENABLED", True, raising=False)

    async def fake_do_search(dry_run=False):
        captured.update(
            dry_run=dry_run,
            queries=list(config.SEARCH_QUERIES),
            pages=config.SEARCH_PAGES,
            superjob=config.SUPERJOB_ENABLED,
            habr=config.HABR_ENABLED,
            geekjob=config.GEEKJOB_ENABLED,
        )
        return {"found": 1, "applied": 0, "skipped": 1, "source_stats": {}}

    calls = []

    async def fake_office_log(action, message, status):
        calls.append((action, message, status))

    monkeypatch.setattr(agent, "do_search", fake_do_search)
    monkeypatch.setattr(agent, "office_log", fake_office_log)

    result = asyncio.run(agent.do_fresh_search())

    assert result["found"] == 1
    assert captured == {
        "dry_run": False,
        "queries": ["manual QA", "QA тестировщик"],
        "pages": 1,
        "superjob": False,
        "habr": False,
        "geekjob": False,
    }
    assert config.SEARCH_QUERIES == ["wide query"]
    assert config.SEARCH_PAGES == 3
    assert config.SUPERJOB_ENABLED is True
    assert config.HABR_ENABLED is True
    assert config.GEEKJOB_ENABLED is True
    assert calls[0][0] == "fresh_search_start"
    assert calls[-1][0] == "fresh_search_done"


def test_do_fresh_search_disabled(monkeypatch):
    monkeypatch.setattr(config, "HH_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HH_FRESH_SEARCH_ENABLED", False, raising=False)

    async def fail_do_search(dry_run=False):
        raise AssertionError("do_search should not run")

    monkeypatch.setattr(agent, "do_search", fail_do_search)

    result = asyncio.run(agent.do_fresh_search())

    assert result["found"] == 0
    assert result["note"] == "fresh hh disabled"
