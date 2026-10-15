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
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=answer))])

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


@pytest.mark.parametrize("original,quote", [
    (f"API {dash} Postman", "API - Postman") for dash in "‐‑‒–—―"
] + [
    ('Проверяю «API» в “Postman”', 'Проверяю "API" в "Postman"'),
    ("Проверяю ‘API’ в ’Postman’", "Проверяю 'API' в 'Postman'"),
    ("REST\u00a0API\u202fPostman", "REST API Postman"),
    ("тести\u00adрование API…", "тестирование API..."),
])
def test_typographic_variants_preserve_literal_evidence(original, quote):
    assert grounding.validate_check(approved(quote), SENTENCES, {"resume": original}).ok


@pytest.mark.parametrize("quote", [
    "Проверяю GraphQL в Postman.",
    "Есть опыт работы с: Postman; Chrome DevTools; Charles Proxy",
    "Postman; Charles Proxy",
])
def test_normalization_does_not_accept_absent_or_spliced_quotes(quote):
    sources = {"resume": "Есть опыт работы с:\n- Postman;\n- Swagger;\n"
                         "- Chrome DevTools;\n- Charles Proxy"}
    assert grounding.validate_check(approved(quote), SENTENCES, sources).reason == "nonliteral_quote"


@pytest.mark.parametrize("quote,source,reason", [
    ("PRIVATE_QUOTE_TEXT", "resume", "nonliteral_quote"),
    ("PRIVATE_QUOTE_TEXT", "PRIVATE_SOURCE_NAME", "unknown_source"),
    ("x", "resume", "quote_too_short"),
])
def test_source_mismatch_logs_only_safe_metadata(caplog, quote, source, reason):
    grounding.validate_check(approved(quote, source), SENTENCES, SOURCES)
    assert f"quote_chars={len(quote)} reason={reason}" in caplog.text
    assert f"source={'resume' if source == 'resume' else 'unknown'}" in caplog.text
    assert "PRIVATE_" not in caplog.text
    assert SOURCES["resume"] not in caplog.text


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
    assert grounding.validate_check(payload, sentences, sources).reason == "nonliteral_quote"
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


def test_constraints_cannot_supply_evidence_even_if_verifier_cites_them():
    claim = "Проверяю REST API в Postman."
    client = client_for(json.dumps(approved(claim, "constraints")))
    result = asyncio.run(grounding.verify_cover_letter(
        " ".join(SENTENCES), SOURCES, client, "model", constraints=claim))
    assert not result.ok and result.reason == "unknown_source"
    assert len(client.calls) == 1
    request = json.loads(client.calls[0]["messages"][1]["content"])
    assert request["constraints"] == claim
    assert "constraints" not in request["sources"]
    assert "даже при дословной цитате" in client.calls[0]["messages"][0]["content"]


def test_nonliteral_quote_retries_full_audit_with_identical_snapshot():
    client = client_for(json.dumps(approved("Invented quote")), json.dumps(approved()))
    result = asyncio.run(grounding.verify_cover_letter(
        " ".join(SENTENCES), SOURCES, client, "model", constraints="Keep constraints"))
    assert result.ok and result.reason == "verified"
    assert len(client.calls) == 2
    assert client.calls[0]["messages"][1] == client.calls[1]["messages"][1]
    assert "Повтори ПОЛНУЮ проверку" in client.calls[1]["messages"][0]["content"]


@pytest.mark.parametrize("second", [
    approved("Invented quote"),
    {"verdict": "unsupported"},
    {"verdict": "supported", "sentences": []},
    {"verdict": "supported", "sentences": [
        {"index": 0, "supported": True, "evidence": []},
        {"index": 1, "supported": True, "evidence": []},
    ]},
    approved("Postman 30"),
])
def test_audit_retry_never_bypasses_full_validation(second):
    client = client_for(json.dumps(approved("Invented quote")), json.dumps(second))
    result = asyncio.run(grounding.verify_cover_letter(" ".join(SENTENCES), SOURCES, client, "model"))
    assert not result.ok
    assert len(client.calls) == 2


def test_retry_rejects_invented_numbers_even_with_literal_quotes():
    client = client_for(json.dumps(approved("Invented quote")), json.dumps(approved()))
    result = asyncio.run(grounding.verify_cover_letter(
        "Проверил 30 API. Обсудим детали?", SOURCES, client, "model"))
    assert result.reason == "number_mismatch"
    assert len(client.calls) == 2


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
    monkeypatch.setattr(prompt_blocks, "build_cover_facts_blocks", lambda: ("", ""))
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
    client = client_for(draft, '{"verdict":"unsupported","sentences":[]}',
                        draft, '{"verdict":"unsupported","sentences":[]}')
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "Manual QA", "snippet": draft, "id": "unit-only"}))
    assert result == matcher._fallback_cover_letter({})
    meta = matcher.analyze_cover_letter(result)
    assert meta["overclaim_guard"] and meta["fallback_cover_letter"]
    assert meta["grounding_status"] == "unsupported_claims"
    assert len(client.calls) == 4


def test_actual_generator_rejects_unverifiable_draft(isolated_candidate, monkeypatch):
    client = client_for(" ".join(SENTENCES), RuntimeError("API down"))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "QA", "id": "unit-only"}))
    assert matcher.analyze_cover_letter(result)["grounding_status"] == "verifier_error"
    assert result == matcher._fallback_cover_letter({})


def test_nonliteral_quote_is_check_failure_not_overclaim(isolated_candidate, monkeypatch):
    client = client_for(" ".join(SENTENCES), json.dumps(approved("Absent quote")))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "QA", "id": "unit-only"}))
    meta = matcher.analyze_cover_letter(result)
    assert result == matcher._fallback_cover_letter({})
    assert meta["grounding_status"] == "nonliteral_quote"
    assert meta["grounding_check_failed"] and meta["fallback_cover_letter"]
    assert not meta["overclaim_guard"]
    assert len(client.calls) == 3


def test_hh_retry_fallback_does_not_bypass_grounding_with_invented_skills():
    import agent
    text = agent._build_hh_retry_cover_letter({"title": "Электрик", "company": "Unit Company"})
    assert "Электрик" in text
    for unsupported in ("QA", "web/API", "DevTools", "Postman", "автотест", "бэкграунд"):
        assert unsupported not in text


def unsupported_first():
    return {"verdict": "unsupported", "sentences": [
        {"index": 0, "supported": False, "evidence": []},
        {"index": 1, "supported": True, "evidence": []},
    ]}


def test_vacancy_only_hours_skip_verifier_and_trigger_one_rewrite(isolated_candidate, monkeypatch):
    draft = "Готов работать 4-6 часов в день. Обсудим детали?"
    revised = " ".join(SENTENCES)
    client = client_for(draft, revised, json.dumps(approved()))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "QA", "snippet": "4-6 часов в день"}))
    assert result == revised
    assert len(client.calls) == 3
    assert [call["max_tokens"] for call in client.calls] == [2000, 2000, 2500]
    rewrite = client.calls[1]["messages"][0]["content"]
    assert draft.split(" Обсудим")[0] in rewrite
    assert '"rejection_reason": "unconfirmed_number"' in rewrite
    assert json.loads(client.calls[2]["messages"][1]["content"])["sentences"] == SENTENCES
    assert matcher.analyze_cover_letter(result)["grounding_status"] == "verified"


@pytest.mark.parametrize("second", ["Готов работать 6 часов в день. Обсудим детали?",
                                    "Ускорил проверку API. Обсудим детали?"])
def test_failed_rewrite_falls_back_without_third_author_attempt(isolated_candidate, monkeypatch, second):
    first = "Готов работать 4-6 часов в день. Обсудим детали?"
    client = client_for(first, second, json.dumps(unsupported_first()))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "QA", "snippet": first}))
    assert result == matcher._fallback_cover_letter({})
    assert matcher.analyze_cover_letter(result)["overclaim_guard"]
    assert sum(call["max_tokens"] == 2000 for call in client.calls) == 2
    assert sum(call["max_tokens"] == 2500 for call in client.calls) == (0 if "6 часов" in second else 1)


def test_semantic_rejection_is_rewritten_with_exact_rejected_sentences(isolated_candidate, monkeypatch):
    draft = "Создаю чек-листы и проверяю REST API в Postman. Обсудим детали?"
    client = client_for(draft, json.dumps(unsupported_first()), " ".join(SENTENCES), json.dumps(approved()))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "QA", "snippet": "VACANCY_ONLY"}))
    assert result == " ".join(SENTENCES)
    rewrite = client.calls[2]["messages"][0]["content"]
    data = json.loads(rewrite[rewrite.index('{'):])
    assert data["rejected_sentences"] == [draft.split(" Обсудим")[0]]
    assert data["sources"]["resume"] == SOURCES["resume"]
    assert "VACANCY_ONLY" not in rewrite
    first_audit, second_audit = client.calls[1], client.calls[3]
    assert first_audit["messages"][0] == second_audit["messages"][0]
    for key in ("sources", "constraints", "neutral_sentences"):
        assert json.loads(first_audit["messages"][1]["content"])[key] == json.loads(second_audit["messages"][1]["content"])[key]
    assert not matcher.analyze_cover_letter(result)["fallback_cover_letter"]


@pytest.mark.parametrize("second_check,reason", [
    (approved("Invented quote"), "nonliteral_quote"),
    (approved("Postman", "extraction"), "unknown_source"),
    ({"verdict": "supported", "sentences": []}, "incomplete_check"),
    (unsupported_first(), "unsupported_claims"),
    ({"verdict": "supported", "sentences": [
        {"index": 0, "supported": True, "evidence": []},
        {"index": 1, "supported": True, "evidence": []},
    ]}, "missing_evidence"),
])
def test_rewritten_letter_has_no_grounding_exemptions_or_extra_audit(
        isolated_candidate, monkeypatch, second_check, reason):
    draft = "Создаю чек-листы. Обсудим детали?"
    client = client_for(draft, json.dumps(unsupported_first()), " ".join(SENTENCES), json.dumps(second_check))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "QA"}))
    assert result == matcher._fallback_cover_letter({})
    assert matcher.analyze_cover_letter(result)["grounding_status"] == reason
    assert len(client.calls) == 4  # Two authors, two auditors, even on a nonliteral quote.


def test_unknown_source_is_overclaim_and_does_not_retry_same_audit(isolated_candidate, monkeypatch):
    client = client_for(" ".join(SENTENCES), json.dumps(approved("Postman", "extraction")), "")
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "QA"}))
    meta = matcher.analyze_cover_letter(result)
    assert meta["grounding_status"] == "unknown_source"
    assert meta["overclaim_guard"] and not meta["grounding_check_failed"]
    assert [call["max_tokens"] for call in client.calls] == [2000, 2500, 2000]


def test_mechanical_audit_retry_and_author_rewrite_share_two_call_budget(isolated_candidate, monkeypatch):
    client = client_for(" ".join(SENTENCES), json.dumps(approved("Absent")), json.dumps(unsupported_first()))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "QA"}))
    assert result == matcher._fallback_cover_letter({})
    assert len(client.calls) == 3
    assert matcher.analyze_cover_letter(result)["grounding_status"] == "unsupported_claims"


def test_rewritten_numbers_still_require_literal_numeric_evidence(isolated_candidate, monkeypatch):
    monkeypatch.setattr(matcher, "_load_resume", lambda: SOURCES["resume"] + " Проверил 30 сценариев.")
    first = "Создаю чек-листы. Обсудим детали?"
    revised = "Проверил 30 сценариев. Обсудим детали?"
    # 30 exists in candidate sources, but the supplied quote omits it.
    client = client_for(first, json.dumps(unsupported_first()), revised, json.dumps(approved()))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "QA"}))
    assert result == matcher._fallback_cover_letter({})
    assert matcher.analyze_cover_letter(result)["grounding_status"] == "number_mismatch"
    assert len(client.calls) == 4


@pytest.mark.parametrize("claim", [
    "Работаю 4-6 часов в день.", "Работаю четыре часа в день.",
    "Стаж 5 лет.", "Стаж полгода.", "Готов работать 3 дня в неделю.",
    "Снизил число ошибок на 30%.", "Ожидаю 120 000 рублей.",
    "Проверил 47 сценариев.", "Ожидаю $4.", "Стаж двадцать один месяц.",
])
def test_local_number_guard_rejects_unconfirmed_quantities(claim):
    result = grounding.check_draft_numbers(claim, {"resume": "Postman. Стаж 4 года. 6 проектов."})
    assert not result.ok
    assert result.rejected_sentences == (claim,)


@pytest.mark.parametrize("claim,source", [
    ("Стаж 1 год.", "Около 1 года практики."),
    ("Стаж два года.", "Стаж 2 года."),
    ("Ожидаю 120000 рублей.", "Зарплата 120 000 руб."),
    ("Ожидаю 120 тыс. рублей.", "Зарплата 120 000 руб."),
    ("Ожидаю $1000.", "Зарплата 1000 USD."),
    ("Стаж двадцать один месяц.", "Стаж 21 месяц."),
    ("Работаю 4–6 часов в день.", "Готов работать 4-6 часов в день."),
    ("Поддерживаю более 360 тест-кейсов в TestIT.", "Более 360 тест-кейсов в TestIT."),
    ("Снизил число ошибок на 30%.", "Снижение ошибок на 30 процентов."),
    ("Проверяю API в Postman. Обсудим детали?", SOURCES["resume"]),
    ("Направляю резюме на вакансию «QA 2».", SOURCES["resume"]),
])
def test_local_number_guard_keeps_supported_and_neutral_phrases(claim, source):
    assert grounding.check_draft_numbers(claim, {"resume": source}, title="QA 2").ok


def test_number_in_job_title_cannot_excuse_candidate_claim():
    assert not grounding.check_draft_numbers("Работаю QA 2 года.", SOURCES, title="QA 2").ok
