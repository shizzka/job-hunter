import json

import pytest

from llm_utils import parse_llm_json


def test_parse_llm_json_returns_object_from_plain_fenced_and_wrapped_text():
    assert parse_llm_json('{"score": 85}') == {"score": 85}
    assert parse_llm_json('```json\n{"score": 85}\n```') == {"score": 85}
    assert parse_llm_json('thinking first\n{"score": 85}\ndone') == {"score": 85}


@pytest.mark.parametrize("payload", ['[1, 2, 3]', '"ok"', "42", "null", "true"])
def test_parse_llm_json_rejects_non_object_json(payload):
    with pytest.raises(json.JSONDecodeError, match="Expected a JSON object"):
        parse_llm_json(payload)
