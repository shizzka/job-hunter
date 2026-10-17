import json

import pytest

from hh.resume_refresh import extract_resume_numeric_id


def html(state):
    return '<template id="HH-Lux-InitialState">' + json.dumps(state) + '</template>'


@pytest.mark.parametrize("state", [
    {"resume": {"hash": "target", "id": 123}},
    {"resumes": {"123": {"hash": "target"}}},
    {"resumeView": {"resume": {"resumeHash": "target", "resumeId": 123}}},
    {"resume": {"id": "target", "resumeId": 123}},
])
def test_numeric_identity_requires_explicit_matching_resume_pair(state):
    assert extract_resume_numeric_id(html(state), "target") == 123


@pytest.mark.parametrize("state", [
    {"user": {"id": 123}, "resume": {"title": "target"}},
    {"resume": {"id": 123}},
    {"random": {"hash": "target", "id": 123}},
    {"resume": {"hash": "other", "id": 123}},
    {"resume": {"hash": "target", "id": 123}, "resumes": {"124": {"hash": "target"}}},
    {"resume": {"hash": "target", "resumeHash": "wrong", "id": 123}},
    {"resume": {"hash": "target", "id": True}},
    {"resume": {"hash": "target", "id": 0}},
    {"resumes": [{"hash": "target", "id": 123}, {"hash": "other", "id": 123}]},
    {"resumes": [{"id": "target", "resumeId": 123}, {"id": "other", "resumeId": 123}]},
])
def test_unknown_or_ambiguous_numeric_identity_stays_unknown(state):
    assert extract_resume_numeric_id(html(state), "target") is None
