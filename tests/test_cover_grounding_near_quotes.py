"""Near-literal cover evidence, without changing semantic or answer gates."""
import asyncio
import copy
import json
import logging

import pytest

import answer_grounding
import cover_grounding as grounding
import matcher
import prompt_blocks
from tests.test_cover_grounding import approved, client_for, isolated_candidate


SOURCE = "Проверяю REST API в Postman и составляю подробные отчеты о найденных ошибках."
SENTENCES = [SOURCE, "Обсудим детали?"]
SOURCES = {"resume": SOURCE}
REORDERED = SOURCE.replace("REST API", "API REST")


@pytest.fixture(autouse=True)
def default_cover_threshold(monkeypatch):
    monkeypatch.setenv("COVER_GROUNDING_QUOTE_MIN_SIMILARITY", "0.85")


def verify(payload, *, sentences=SENTENCES, sources=SOURCES, constraints=""):
    return asyncio.run(grounding.verify_cover_letter(
        " ".join(sentences), sources, client_for(json.dumps(payload)), "offline",
        constraints=constraints, max_attempts=1))


@pytest.mark.parametrize("quote,score,accepted", [
    (REORDERED, 11 / 12, True),
    (SOURCE.replace("Postman и", "Postman, и"), 11 / 12, True),
    (SOURCE.replace("Проверяю", "Проверял"), 22 / 23, True),
    (SOURCE.replace("Проверяю REST API", "Разрабатываю облачные сервисы"), 18 / 22, False),
    ("Создаю дизайн интерфейсов для мобильных банковских продуктов на платформе Android.", 0, False),
    (SOURCE.replace("Postman и", "Postman и не"), 24 / 25, False),
])
def test_near_quote_examples_and_scores(quote, score, accepted):
    _, actual = grounding._near_quote_fragment(grounding._normalize(quote), grounding._normalize(SOURCE))
    assert actual == pytest.approx(score)
    result = verify(approved(quote))
    assert result.ok is accepted
    assert result.reason == ("verified" if accepted else "nonliteral_quote")


@pytest.mark.parametrize("source,quote", [
    ("тестирование веб приложений", "тестирование веб приложения"),
    ("Проверяю REST API в Postman и DevTools", "Проверял REST API в Postman и DevTools"),
    ("REST API — тестирование", "REST API - проверка"),
])
def test_short_quotes_require_exact_copy(source, quote):
    assert verify(approved(quote), sources={"resume": source}).reason == "nonliteral_quote"
    assert verify(approved(source), sources={"resume": source}).ok


def test_eight_word_boundary_allows_minor_punctuation_change():
    source = "Проверяю REST API в Postman и составляю отчеты."
    quote = source.replace("Postman и", "Postman, и")
    assert len(source.split()) == 8
    assert verify(approved(quote), sources={"resume": source}).ok


def test_window_search_uses_continuous_part_of_larger_source():
    sources = {"resume": "Дополнительные сведения кандидата. " + SOURCE + " Дополнительный раздел резюме."}
    fragment, score = grounding._near_quote_fragment(grounding._normalize(REORDERED),
                                                   grounding._normalize(sources["resume"]))
    assert fragment == grounding._normalize(SOURCE)
    assert score == pytest.approx(11 / 12)
    assert verify(approved(REORDERED), sources=sources).ok


def test_matching_does_not_join_distant_source_fragments():
    sources = {"resume": "Проверяю REST API в Postman. " + "Посторонний раздел. " * 20
               + "Составляю подробные отчеты о найденных ошибках."}
    assert verify(approved(REORDERED), sources=sources).reason == "nonliteral_quote"


@pytest.mark.parametrize("quote_number", ["30", "10"])
def test_numbers_are_checked_against_real_fragment_not_model_quote(quote_number):
    source = "Проверил 10 сценариев REST API в Postman и составил подробные отчеты об ошибках для команды разработки."
    quote = source.replace("10", quote_number).replace("подробные", "подробный")
    sentence = source.replace("10", "30")
    # Invented 30 exists elsewhere in this source but outside the matched window.
    sources = {"resume": source + " Другие сведения: 30 дней обучения."}
    fragment, score = grounding._near_quote_fragment(grounding._normalize(quote),
                                                   grounding._normalize(sources["resume"]))
    assert score >= 0.85 and "30" not in fragment and "10" in fragment
    assert verify(approved(quote), sentences=[sentence, SENTENCES[1]], sources=sources).reason == "number_mismatch"


def test_real_source_digits_can_support_claim_even_when_model_quote_changes_them():
    source = "Проверил 10 сценариев REST API в Postman и составил подробные отчеты об ошибках."
    quote = source.replace("10", "30")
    assert verify(approved(quote), sentences=[source, SENTENCES[1]], sources={"resume": source}).ok


@pytest.mark.parametrize("mutation,reason", [
    ("verdict", "unsupported_claims"),
    ("supported_false", "unsupported_claims"),
    ("supported_string", "unsupported_claims"),
    ("supported_one", "unsupported_claims"),
    ("empty_evidence", "missing_evidence"),
    ("unknown_source", "unknown_source"),
    ("tiny_quote", "nonliteral_quote"),
    ("constraints_source", "unknown_source"),
])
def test_relaxation_keeps_hard_rejection_gates(mutation, reason):
    payload = approved(REORDERED)
    row = payload["sentences"][0]
    if mutation == "verdict":
        payload["verdict"] = "unsupported"
    elif mutation.startswith("supported_"):
        row["supported"] = {"supported_false": False, "supported_string": "true", "supported_one": 1}[mutation]
    elif mutation == "empty_evidence":
        row["evidence"] = []
    elif mutation == "unknown_source":
        row["evidence"][0]["source"] = "other_profile"
    elif mutation == "constraints_source":
        row["evidence"][0]["source"] = "constraints"
    else:
        row["evidence"][0]["quote"] = "API"[:2]
    assert verify(payload, constraints=REORDERED).reason == reason


def test_constraints_rejection_and_audit_instructions_are_preserved():
    payload = approved(REORDERED)
    payload["sentences"][0]["supported"] = False
    client = client_for(json.dumps(payload))
    constraint = "Не утверждать опыт проверки REST API."
    result = asyncio.run(grounding.verify_cover_letter(
        " ".join(SENTENCES), SOURCES, client, "offline", constraints=constraint))
    assert result.reason == "unsupported_claims"
    call = client.calls[0]
    assert json.loads(call["messages"][1]["content"])["constraints"] == constraint
    assert "Запреты и границы" in call["messages"][0]["content"]
    assert "даже при дословной цитате" in call["messages"][0]["content"]
    assert len(client.calls) == 1


@pytest.mark.parametrize("threshold", ["1.0", "abc", "-1", "2", "nan", "inf", ""])
def test_strict_or_invalid_env_threshold_rejects_near_quotes(monkeypatch, threshold):
    monkeypatch.setenv("COVER_GROUNDING_QUOTE_MIN_SIMILARITY", threshold)
    assert verify(approved(REORDERED)).reason == "nonliteral_quote"
    assert verify(approved(SOURCE)).ok


def test_configured_threshold_is_used_without_rounding(monkeypatch):
    monkeypatch.setenv("COVER_GROUNDING_QUOTE_MIN_SIMILARITY", "0.92")
    assert verify(approved(REORDERED)).reason == "nonliteral_quote"
    monkeypatch.setenv("COVER_GROUNDING_QUOTE_MIN_SIMILARITY", str(11 / 12))
    assert verify(approved(REORDERED)).ok


def test_answer_validation_stays_literal_even_with_relaxed_cover_env():
    check = approved(REORDERED)
    check["sentences"].pop()  # Answer has one sentence.
    payload = {"verdict": "supported", "answers": [
        {"index": 0, "supported": True, "sentences": check["sentences"]}]}
    answers = [{"index": 0, "answer": SOURCE}]
    assert answer_grounding.validate_answer_check(payload, answers, SOURCES) == set()
    literal = copy.deepcopy(payload)
    literal["answers"][0]["sentences"][0]["evidence"][0]["quote"] = SOURCE
    assert answer_grounding.validate_answer_check(literal, answers, SOURCES) == {0}
    assert grounding.validate_check(approved(REORDERED), SENTENCES, SOURCES).reason == "nonliteral_quote"


@pytest.mark.parametrize("source", ["resume", "PRIVATE_SOURCE_NAME"])
def test_near_acceptance_logs_only_safe_metadata(caplog, source):
    with caplog.at_level(logging.INFO, logger="cover_grounding"):
        assert verify(approved(REORDERED, source), sources={source: SOURCE}).ok
    assert "near-literal evidence accepted" in caplog.text
    assert f"source={'resume' if source == 'resume' else 'unknown'}" in caplog.text
    assert f"quote_chars={len(REORDERED)} similarity=0.917" in caplog.text
    assert SOURCE not in caplog.text and REORDERED not in caplog.text
    assert "PRIVATE_" not in caplog.text


def test_offline_live_case_keeps_real_letter_instead_of_stub(isolated_candidate, monkeypatch):
    monkeypatch.setattr(matcher, "_load_resume", lambda: SOURCE)
    client = client_for(" ".join(SENTENCES), json.dumps(approved(REORDERED)))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "Manual QA", "id": "offline-letters-1"}))
    assert result == " ".join(SENTENCES)
    meta = matcher.analyze_cover_letter(result)
    assert meta["grounding_status"] == "verified"
    assert not meta["fallback_cover_letter"]
    assert len(client.calls) == 2  # One draft, one successful audit; no rewrite.


def test_offline_generator_keeps_constraints_rejection(isolated_candidate, monkeypatch):
    monkeypatch.setattr(matcher, "_load_resume", lambda: SOURCE)
    constraint = "Не утверждать опыт проверки REST API."
    monkeypatch.setattr(prompt_blocks, "build_cover_facts_blocks", lambda: ("", constraint))
    payload = approved(REORDERED)
    payload["sentences"][0]["supported"] = False
    client = client_for(" ".join(SENTENCES), json.dumps(payload), "")
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "Manual QA", "id": "offline-constraints"}))
    assert matcher.analyze_cover_letter(result)["fallback_cover_letter"]
    assert result != " ".join(SENTENCES)
    request = json.loads(client.calls[1]["messages"][1]["content"])
    assert request["constraints"] == constraint
    assert "constraints" not in request["sources"]
