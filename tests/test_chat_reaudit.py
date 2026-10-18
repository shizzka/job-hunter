"""B2: a human-approved chat draft gets one re-audit when the auditor's citations were sloppy;
strict paths keep a single audit and invented digits never pass."""
import asyncio
import json
from types import SimpleNamespace

from answer_grounding import verify_answers

SOURCES = {"resume": "Тестирую веб-приложения и API с 2019 года, пишу тест-кейсы в TestRail."}
ANSWER = [{"index": 0, "question": "Опыт?", "answer": "Тестирую веб-приложения и API с 2019 года."}]


def audit(source, quote):
    return json.dumps({"verdict": "supported", "answers": [{"index": 0, "supported": True, "sentences": [
        {"index": 0, "supported": True, "evidence": [{"source": source, "quote": quote}]}]}]}, ensure_ascii=False)


class Client:
    def __init__(self, *contents):
        self.contents, self.calls = list(contents), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])


def run(client, threshold):
    diagnostics = {}
    accepted = asyncio.run(verify_answers(ANSWER, SOURCES, client, "m", diagnostics=diagnostics,
                                          quote_min_similarity=threshold))
    return accepted, diagnostics


def test_draft_reaudit_rescues_sloppy_citation():
    client = Client(audit("опыт_работы", "Тестирую веб-приложения и API с 2019 года"),
                    audit("resume", "Тестирую веб-приложения и API с 2019 года"))
    accepted, diagnostics = run(client, 0.85)
    assert accepted == {0} and len(client.calls) == 2
    assert "Предыдущая проверка отклонена" in client.calls[1]["messages"][0]["content"]


def test_russian_source_name_is_the_same_source():
    accepted, _ = run(Client(audit("Резюме", "Тестирую веб-приложения и API с 2019 года")), 1.0)
    assert accepted == {0}


def test_strict_path_keeps_one_audit():
    client = Client(audit("опыт_работы", "Тестирую веб-приложения и API с 2019 года"))
    accepted, diagnostics = run(client, 1.0)
    assert accepted == set() and len(client.calls) == 1 and diagnostics["reason"] == "unknown_source"


def test_digits_must_be_quoted_even_after_reaudit():
    answer_quote = "Тестирую веб-приложения и API"
    client = Client(audit("resume", answer_quote), audit("resume", answer_quote))
    accepted, diagnostics = run(client, 0.85)
    assert accepted == set() and len(client.calls) == 2 and diagnostics["reason"] == "number_mismatch"
