"""Gemini must accept the model identifier the previous provider used.

A fallback chain reaches gemini carrying groq's identifier (openai/gpt-oss-120b).
Forwarding that name verbatim made gemini answer NotFoundError, so the chain fell
through to the local model and the cover letter degraded to a stub.
"""
from __future__ import annotations

import llm_client


ENV = {"GEMINI_STRONG_MODEL": "gemini-3.1-flash-lite",
       "GEMINI_FAST_MODEL": "gemini-3.1-flash-lite"}


def test_groq_identifiers_resolve_to_a_real_gemini_model():
    aliases = llm_client._gemini_model_aliases(ENV)
    assert aliases["openai/gpt-oss-120b"] == "gemini-3.1-flash-lite"
    assert aliases["openai/gpt-oss-20b"] == "gemini-3.1-flash-lite"


def test_existing_identifiers_still_resolve():
    aliases = llm_client._gemini_model_aliases(ENV)
    assert aliases["gpt-oss:120b"] == "gemini-3.1-flash-lite"
    assert aliases["gpt-oss:20b"] == "gemini-3.1-flash-lite"


def test_unknown_names_are_left_untouched():
    aliases = llm_client._gemini_model_aliases(ENV)
    assert "some-unlisted-model" not in aliases
