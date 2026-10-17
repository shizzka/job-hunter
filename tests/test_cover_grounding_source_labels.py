import pytest

import cover_grounding as grounding

SENTENCE = "Имею более 10 лет опыта в электромонтаже."
QUOTE = "более 10 лет в электромонтаже и слаботочных системах"
SOURCES = {
    "resume": "Электромонтажник. Монтаж кабельных трасс.",
    "facts": "Город: Санкт-Петербург.",
    "knowledge": "### facts.json > LIMIT / ALLOWED_WORDING\n" + QUOTE,
}


def payload(source: str) -> dict:
    return {"verdict": "supported", "sentences": [
        {"index": 0, "supported": True, "evidence": [{"source": source, "quote": QUOTE}]},
    ]}


@pytest.mark.parametrize("label", ["facts", "resume", "knowledge",
                                   "knowledge/candidate_facts.md", "facts.json > LIMIT / ALLOWED_WORDING"])
def test_relaxed_audit_finds_quote_in_another_known_source(label):
    result = grounding.validate_check(payload(label), [SENTENCE], SOURCES, quote_min_similarity=0.85)
    assert result.ok, result.reason


@pytest.mark.parametrize("label", ["constraints", "extraction", "hr", ""])
def test_relaxed_audit_still_rejects_unknown_labels(label):
    result = grounding.validate_check(payload(label), [SENTENCE], SOURCES, quote_min_similarity=0.85)
    assert not result.ok and result.reason == "unknown_source"


@pytest.mark.parametrize("label", ["facts", "knowledge/candidate_facts.md"])
def test_strict_audit_keeps_exact_source_contract(label):
    result = grounding.validate_check(payload(label), [SENTENCE], SOURCES)
    assert not result.ok


def test_relaxed_audit_rejects_text_absent_from_every_source():
    data = payload("facts")
    data["sentences"][0]["evidence"][0]["quote"] = "группа допуска IV до и выше 1000 В действует"
    result = grounding.validate_check(data, [SENTENCE], SOURCES, quote_min_similarity=0.85)
    assert not result.ok
