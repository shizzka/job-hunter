import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import relevance_verifier as verifier


@pytest.fixture
def setup(monkeypatch):
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"verdict":"review","reason":"недостаточно опыта"}'))]))
    monkeypatch.setattr(verifier, 'get_llm_client', lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(verifier.matcher, '_load_resume', lambda: 'QA resume')
    monkeypatch.setattr(verifier.config, 'HH_VERIFIER_SHADOW_ENABLED', True)
    events = []
    monkeypatch.setattr(verifier.analytics, '_append_event', events.append)
    return create, events


def test_shadow_is_bounded_and_does_not_change_decision(setup):
    create, events = setup
    checker = verifier.ShadowVerifier(max_checks=1)
    evaluation = {'score': 80, 'should_apply': True}
    original = deepcopy(evaluation)
    async def run():
        result = await checker.check('run', {'id': '1'}, '', evaluation)
        assert result['verdict'] == 'review'
        assert await checker.check('run', {'id': '2'}, '', evaluation) is None
    asyncio.run(run())
    assert evaluation == original
    create.assert_awaited_once()
    assert events[0]['primary_should_apply'] is True


@pytest.mark.parametrize('score', [0, 69, 91, None])
def test_outside_borderline_makes_no_request(setup, score):
    create, events = setup
    assert asyncio.run(verifier.ShadowVerifier().check('r', {'id': '1'}, '', {'score': score})) is None
    create.assert_not_awaited()


def test_provider_failure_does_not_change_decision(setup):
    create, events = setup
    create.side_effect = RuntimeError('offline')
    evaluation = {'score': 75, 'should_apply': True}
    result = asyncio.run(verifier.ShadowVerifier().check('r', {'id': '1'}, '', evaluation))
    assert result['verdict'] == 'unknown'
    assert evaluation['should_apply'] is True
    assert events[0]['error_kind'] == 'RuntimeError'


def test_disabled_and_hard_rejects_make_no_request(setup, monkeypatch):
    create, _ = setup
    checker = verifier.ShadowVerifier()
    assert asyncio.run(checker.check('r', {}, '', {'score': 85, 'hard_flags': ['non_qa']})) is None
    monkeypatch.setattr(verifier.config, 'HH_VERIFIER_SHADOW_ENABLED', False)
    assert asyncio.run(checker.check('r', {}, '', {'score': 85})) is None
    create.assert_not_awaited()


def test_malformed_json_is_unknown(setup):
    create, events = setup
    create.return_value.choices[0].message.content = '{"verdict":"sure"}'
    result = asyncio.run(verifier.ShadowVerifier().check('r', {'id': '1'}, '', {'score': 80}))
    assert result['verdict'] == 'unknown'


@pytest.mark.parametrize('score', [70, 90])
def test_inclusive_score_boundaries(setup, score):
    create, _ = setup
    assert asyncio.run(verifier.ShadowVerifier().check('r', {'id': '1'}, '', {'score': score}))['verdict'] == 'review'
    create.assert_awaited_once()


def test_retry_and_other_sources_are_skipped(setup):
    create, _ = setup
    checker = verifier.ShadowVerifier()
    for vacancy in [{'source': 'hh', '_hh_retry': True}, {'source': 'habr'}]:
        assert asyncio.run(checker.check('r', vacancy, '', {'score': 80})) is None
    create.assert_not_awaited()
