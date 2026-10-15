"""The verification timeout must follow configuration, not a hardcoded literal.

Verification carries the whole knowledge base, so a cloud provider can need far
longer than a short chat completion: gemini-3.1-flash-lite needed 46.5s on the
real 22k-character prompt, while the previous hardcoded 40s rejected it as a
transport failure and silently replaced the letter with a stub.
"""
from __future__ import annotations

import importlib

import config
import cover_grounding


def test_grounding_timeout_follows_configuration(monkeypatch):
    monkeypatch.setattr(config, "LLM_GROUNDING_TIMEOUT_SECONDS", 120)
    assert cover_grounding._grounding_timeout() == 120


def test_grounding_timeout_reads_the_environment(monkeypatch):
    monkeypatch.setenv("LLM_GROUNDING_TIMEOUT_SECONDS", "97")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.LLM_GROUNDING_TIMEOUT_SECONDS == 97
        assert cover_grounding._grounding_timeout() == 97
    finally:
        monkeypatch.delenv("LLM_GROUNDING_TIMEOUT_SECONDS", raising=False)
        importlib.reload(config)


def test_grounding_timeout_never_drops_below_one_second(monkeypatch):
    monkeypatch.setattr(config, "LLM_GROUNDING_TIMEOUT_SECONDS", 0)
    assert cover_grounding._grounding_timeout() == 1


def test_no_hardcoded_timeout_remains_in_the_verifiers():
    for name in ("cover_grounding.py", "answer_grounding.py"):
        with open(name, encoding="utf-8") as handle:
            assert "timeout=40)" not in handle.read(), name
