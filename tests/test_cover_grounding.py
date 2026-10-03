import asyncio
import json
from types import SimpleNamespace

import pytest

import cover_grounding as grounding
import matcher
import prompt_blocks


def client_for(*answers):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        answer = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), calls=calls)


def approved(quote="Проверяю REST API в Postman.", source="resume"):
    return {"verdict": "supported", "sentences": [
        {"index": 0, "supported": True, "evidence": [{"source": source, "quote": quote}]},
        {"index": 1, "supported": True, "evidence": []},
    ]}


SENTENCES = ["Проверяю REST API в Postman.", "Обсудим детали?"]
SOURCES = {"resume": "Проверяю REST API в Postman."}


def test_literal_evidence_with_full_sentence_coverage_is_accepted():
    result = grounding.validate_check(approved(), SENTENCES, SOURCES)
    assert result.ok and result.reason == "verified"


@pytest.mark.parametrize("payload", [
    None, [], {}, {"verdict": "unsupported"},
    {"verdict": "supported", "sentences": []},
    {"verdict": "supported", "sentences": [None, None]},
    {"verdict": "supported", "sentences": [{"index": True}, {"index": 1}]},
    {"verdict": "supported", "sentences": [{"index": -1}, {"index": 1}]},
    {"verdict": "supported", "sentences": [{"index": 100}, {"index": 1}]},
])
def test_malformed_or_incomplete_checks_fail_closed(payload):
    assert not grounding.validate_check(payload, SENTENCES, SOURCES).ok


@pytest.mark.parametrize("mutation", ["duplicate", "unsupported", "string_bool", "no_evidence", "bad_evidence", "quote_type", "source_type"])
def test_check_cannot_skip_or_forge_a_claim(mutation):
    payload = approved()
    row = payload["sentences"][0]
    if mutation == "duplicate":
        payload["sentences"][1]["index"] = 0
    elif mutation == "unsupported":
        row["supported"] = False
    elif mutation == "string_bool":
        row["supported"] = "true"
    elif mutation == "no_evidence":
        row["evidence"] = []
    elif mutation == "bad_evidence":
        row["evidence"] = "resume"
    elif mutation == "quote_type":
        row["evidence"][0]["quote"] = ["Postman"]
    else:
        row["evidence"][0]["source"] = ["resume"]
    assert not grounding.validate_check(payload, SENTENCES, SOURCES).ok


@pytest.mark.parametrize("quote,source", [("ADMIN_ONLY", "resume"), ("Postman", "other_profile"), (" ", "resume"), ("x", "resume")])
def test_evidence_must_exist_in_this_snapshot(quote, source):
    assert not grounding.validate_check(approved(quote, source), SENTENCES, SOURCES).ok


def test_invented_numbers_are_rejected_even_if_verifier_says_supported():
    sentences = ["Сократил число ошибок на 30%.", "Обсудим детали?"]
    assert grounding.validate_check(approved(), sentences, SOURCES).reason == "number_mismatch"


def test_spliced_evidence_is_rejected_but_separate_literal_quotes_are_allowed():
    sources = {"resume": "Проверяю REST API. Использую DevTools. Работаю в Postman."}
    sentences = ["Проверяю REST API и работаю в Postman.", "Обсудим детали?"]
    payload = approved("Проверяю REST API. Работаю в Postman.")
    assert grounding.validate_check(payload, sentences, sources).reason == "source_mismatch"
    payload["sentences"][0]["evidence"] = [
        {"source": "resume", "quote": "Проверяю REST API."},
        {"source": "resume", "quote": "Работаю в Postman."},
    ]
    assert grounding.validate_check(payload, sentences, sources).ok


@pytest.mark.parametrize("content", ["not JSON", "[]", "null", '{"verdict":"unsupported"}'])
def test_verifier_output_errors_reject_draft(content):
    client = client_for(content)
    result = asyncio.run(grounding.verify_cover_letter(" ".join(SENTENCES), SOURCES, client, "test-model"))
    assert not result.ok


def test_verifier_has_independent_context_and_zero_temperature():
    client = client_for(json.dumps(approved()))
    result = asyncio.run(grounding.verify_cover_letter(" ".join(SENTENCES), SOURCES, client, "verification-model"))
    assert result.ok
    call = client.calls[0]
    assert call["model"] == "verification-model" and call["temperature"] == 0
    assert call["response_format"] == {"type": "json_object"}
    assert "НЕ автор письма" in call["messages"][0]["content"]
    assert "Общие навыки НЕ доказывают" in call["messages"][0]["content"]
    payload = json.loads(call["messages"][1]["content"])
    assert payload["sources"] == SOURCES
    assert payload["sentences"] == SENTENCES
    assert "vacancy" not in payload


def test_verifier_failure_rejects_without_leaking_transport_body(caplog):
    client = client_for(RuntimeError("PRIVATE_CANDIDATE_EMAIL"))
    result = asyncio.run(grounding.verify_cover_letter(" ".join(SENTENCES), SOURCES, client, "model"))
    assert result.reason == "verifier_error"
    assert "PRIVATE_CANDIDATE_EMAIL" not in caplog.text


@pytest.mark.parametrize("text,sources,reason", [
    ("Обсудим детали?", {}, "neutral_only"),
    ("У меня есть опыт.", {}, "missing_sources"),
    ("", SOURCES, "invalid_draft"),
    ("x" * 1501, SOURCES, "invalid_draft"),
])
def test_local_fast_paths_do_not_call_api(text, sources, reason):
    client = client_for(RuntimeError("should not call"))
    result = asyncio.run(grounding.verify_cover_letter(text, sources, client, "model"))
    assert result.reason == reason
    assert not client.calls


@pytest.fixture
def isolated_candidate(monkeypatch):
    monkeypatch.setattr(matcher, "_load_resume", lambda: SOURCES["resume"])
    monkeypatch.setattr(prompt_blocks, "build_facts_block", lambda: "")
    monkeypatch.setattr(prompt_blocks, "build_profile_note_block", lambda: "")
    monkeypatch.setattr(prompt_blocks, "build_knowledge_base_block", lambda **kwargs: "")

    async def filtered(*args, **kwargs):
        return ""

    monkeypatch.setattr(prompt_blocks, "build_filtered_kb_block", filtered)


def test_actual_generator_keeps_a_verified_letter(isolated_candidate, monkeypatch):
    letter = " ".join(SENTENCES)
    client = client_for(letter, json.dumps(approved()))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "Manual QA", "id": "unit-only"}))
    assert result == letter
    meta = matcher.analyze_cover_letter(result)
    assert meta["grounding_status"] == "verified" and not meta["fallback_cover_letter"]
    assert len(client.calls) == 2


@pytest.mark.parametrize("draft", [
    "Нашёл ошибку регистрации в последнем проекте. Обсудим детали?",
    "Эти находки сократили число отклонённых баг-репортов. Обсудим детали?",
    "Ускорил воспроизведение ошибок веб-форм. Обсудим детали?",
    "У меня технический бэкграунд. Обсудим детали?",
])
def test_actual_generator_blocks_unsupported_stories(isolated_candidate, monkeypatch, draft):
    client = client_for(draft, '{"verdict":"unsupported","sentences":[]}')
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "Manual QA", "snippet": draft, "id": "unit-only"}))
    assert result == matcher._fallback_cover_letter({})
    meta = matcher.analyze_cover_letter(result)
    assert meta["overclaim_guard"] and meta["fallback_cover_letter"]
    assert meta["grounding_status"] == "unsupported_claims"
    assert len(client.calls) == 2


def test_actual_generator_rejects_unverifiable_draft(isolated_candidate, monkeypatch):
    client = client_for(" ".join(SENTENCES), RuntimeError("API down"))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "QA", "id": "unit-only"}))
    assert matcher.analyze_cover_letter(result)["grounding_status"] == "verifier_error"
    assert result == matcher._fallback_cover_letter({})


def test_hh_retry_fallback_does_not_bypass_grounding_with_invented_skills():
    import agent
    text = agent._build_hh_retry_cover_letter({"title": "Электрик", "company": "Unit Company"})
    assert "Электрик" in text
    for unsupported in ("QA", "web/API", "DevTools", "Postman", "автотест", "бэкграунд"):
        assert unsupported not in text
