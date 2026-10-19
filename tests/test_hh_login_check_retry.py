"""19.10: one failed catalog read must not switch hh off for the whole search."""
import asyncio

import hh_client


def make(monkeypatch, outcomes):
    client = hh_client.HHClient()
    calls = []

    async def once():
        calls.append(1)
        return outcomes.pop(0)

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(client, "_is_logged_in_once", once)
    monkeypatch.setattr(hh_client.asyncio, "sleep", no_sleep)
    return client, calls


def test_transient_failure_is_retried_once(monkeypatch):
    client, calls = make(monkeypatch, [None, True])
    assert asyncio.run(client.is_logged_in()) is True and len(calls) == 2


def test_two_transient_failures_mean_logged_out(monkeypatch):
    client, calls = make(monkeypatch, [None, None])
    assert asyncio.run(client.is_logged_in()) is False and len(calls) == 2


def test_definite_logout_is_not_retried(monkeypatch):
    client, calls = make(monkeypatch, [False])
    assert asyncio.run(client.is_logged_in()) is False and len(calls) == 1


def test_success_needs_no_retry(monkeypatch):
    client, calls = make(monkeypatch, [True])
    assert asyncio.run(client.is_logged_in()) is True and len(calls) == 1
