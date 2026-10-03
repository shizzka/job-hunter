"""Quality traces are private, opt-in and cannot affect scoring/fallback policy."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import llm_client as llm
import ollama_quality_log as quality


def model_response(content='Synthetic answer', finish='stop'):
    return SimpleNamespace(model='qwen3-coder:30b', choices=[SimpleNamespace(
        finish_reason=finish, message=SimpleNamespace(content=content))])


def provider(path, name='ollama', actual='qwen3-coder:30b'):
    return llm.ProviderSpec(name, 'http://local.test/v1', 'synthetic-key',
                            text_fallback_model=actual, timeout_seconds=0.2,
                            quality_log_file=str(path))


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def mocked(monkeypatch, providers, outcome):
    monkeypatch.setattr(llm, '_record_usage', lambda *a, **k: None)
    client = llm.FallbackLLMClient(providers)
    async def create(**kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(client, '_client_for', lambda index: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    return client


def test_complete_prompt_and_answer_paired_private_model_metadata(tmp_path, monkeypatch, caplog):
    path = tmp_path / 'quality' / 'ollama.jsonl'
    prompt = 'Synthetic source ' * 1000
    answer = 'Synthetic answer ' * 1000
    client = mocked(monkeypatch, [provider(path)], model_response(answer))
    asyncio.run(client.chat.completions.create(model='gpt-oss:120b', messages=[
        {'role': 'user', 'content': prompt}], max_tokens=800, temperature=0.2))
    request, response = records(path)
    assert request['event'] == 'request' and response['event'] == 'response'
    assert request['call_id'] == response['call_id'] and request['call_id']
    assert request['provider'] == 'ollama'
    assert request['requested_model'] == 'gpt-oss:120b'
    assert request['actual_model'] == 'qwen3-coder:30b'
    assert request['request']['model'] == 'qwen3-coder:30b'
    assert request['request']['reasoning_effort'] == 'none'
    assert request['request']['messages'][0]['content'] == prompt
    assert response['response']['choices'][0]['message']['content'] == answer
    assert response['response']['choices'][0]['finish_reason'] == 'stop'
    assert response['latency_seconds'] >= 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert prompt not in caplog.text and answer not in caplog.text


def test_headers_keys_and_recognized_body_credentials_are_not_retained(tmp_path, monkeypatch):
    path = tmp_path / 'quality.jsonl'
    secret = 'sk-' + 'x' * 30
    groq_secret = 'gsk_' + 'y' * 30
    client = mocked(monkeypatch, [provider(path)], model_response('Bearer ' + secret))
    asyncio.run(client.chat.completions.create(model='gpt-oss:120b',
        messages=[{'role': 'user', 'content': secret + ' ' + groq_secret}],
        extra_headers={'Authorization': 'transport-private', 'Cookie': 'hhtoken=private'},
        api_key='must-not-log'))
    text = path.read_text()
    for value in (secret, groq_secret, 'transport-private', 'hhtoken=private', 'must-not-log'):
        assert value not in text
    assert '[REDACTED]' in text
    assert 'base_url' not in text and 'extra_headers' not in text


def test_failure_records_class_not_private_error_payload(tmp_path, monkeypatch):
    path = tmp_path / 'quality.jsonl'
    client = mocked(monkeypatch, [provider(path)], ConnectionRefusedError('private-resume-and-key'))
    with pytest.raises(llm.LLMProvidersExhaustedError):
        asyncio.run(client.chat.completions.create(model='gpt-oss:120b', messages=[]))
    request, error = records(path)
    assert error['event'] == 'error' and error['call_id'] == request['call_id']
    assert error['error_kind'] == 'ConnectionRefusedError'
    assert 'private-resume-and-key' not in path.read_text()


def test_malformed_response_is_retained_before_safe_error(tmp_path, monkeypatch):
    path = tmp_path / 'quality.jsonl'
    client = mocked(monkeypatch, [provider(path)], model_response(''))
    with pytest.raises(ValueError):
        asyncio.run(client.chat.completions.create(model='gpt-oss:120b', messages=[]))
    rows = records(path)
    assert [row['event'] for row in rows] == ['request', 'response', 'error']
    assert rows[1]['response']['choices'][0]['message']['content'] == ''


def test_journal_failure_does_not_change_success_or_expose_raw_error(tmp_path, monkeypatch, caplog):
    def fail(*args, **kwargs): raise OSError('private-file-and-key')
    monkeypatch.setattr(quality, 'append_json', fail)
    client = mocked(monkeypatch, [provider(tmp_path / 'quality.jsonl')], model_response())
    result = asyncio.run(client.chat.completions.create(model='gpt-oss:120b', messages=[]))
    assert result.choices[0].message.content == 'Synthetic answer'
    assert 'private-file-and-key' not in caplog.text
    assert 'Ollama quality trace unavailable (OSError)' in caplog.text


@pytest.mark.parametrize('case', ['disabled', 'cloud', 'vision'])
def test_no_implicit_cloud_vision_or_disabled_tracing(tmp_path, monkeypatch, case):
    path = tmp_path / 'quality.jsonl'
    slot = provider(path)
    model = 'gpt-oss:120b'
    if case == 'disabled':
        slot = provider('')
        slot = llm.ProviderSpec('ollama', slot.base_url, slot.api_key,
                                text_fallback_model=slot.text_fallback_model)
    if case == 'cloud':
        slot = llm.ProviderSpec('groq', 'https://cloud.test/v1', 'synthetic', quality_log_file=str(path))
    if case == 'vision':
        model = 'qwen3-vl:235b-instruct'
    client = mocked(monkeypatch, [slot], model_response())
    if case == 'vision':
        with pytest.raises(llm.LLMProvidersExhaustedError):
            asyncio.run(client.chat.completions.create(model=model, messages=[]))
    else:
        asyncio.run(client.chat.completions.create(model=model, messages=[]))
    assert not path.exists()


def test_symlink_log_cannot_replace_or_append_to_another_file(tmp_path, monkeypatch):
    original, link = tmp_path / 'private-original', tmp_path / 'quality.jsonl'
    original.write_text('Original owner bytes')
    link.symlink_to(original)
    client = mocked(monkeypatch, [provider(link)], model_response())
    asyncio.run(client.chat.completions.create(model='gpt-oss:120b', messages=[]))
    assert original.read_text() == 'Original owner bytes'
    assert link.is_symlink()


def test_concurrent_calls_remain_complete_and_pair_by_unique_id(tmp_path, monkeypatch):
    path = tmp_path / 'quality.jsonl'
    client = mocked(monkeypatch, [provider(path)], model_response())
    async def run():
        await asyncio.gather(*(client.chat.completions.create(model='gpt-oss:120b', messages=[])
                               for _ in range(12)))
    asyncio.run(run())
    rows = records(path)
    assert len(rows) == 24
    ids = {row['call_id'] for row in rows}
    assert len(ids) == 12
    for call_id in ids:
        assert [row['event'] for row in rows if row['call_id'] == call_id] == ['request', 'response']


def test_configuration_shared_and_override_trace_paths_are_captured(tmp_path, monkeypatch):
    monkeypatch.delenv('LLM_PROVIDER_ORDER', raising=False)
    monkeypatch.setattr(llm.config, 'LLM_API_KEY', '')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(llm, '_load_provider_env', lambda: {
        'OLLAMA_BASE_URL': 'http://local.test/v1', 'OLLAMA_API_KEY': 'synthetic-one',
        'OLLAMA2_API_KEY': 'synthetic-two', 'OLLAMA3_API_KEY': 'synthetic-three',
        'OLLAMA_FALLBACK_MODEL': 'qwen3-coder:30b', 'OLLAMA2_FALLBACK_MODEL': 'qwen3:8b',
        'OLLAMA_QUALITY_LOG_FILE': '~/quality/ollama.jsonl',
        'OLLAMA2_QUALITY_LOG_FILE': '~/quality/second.jsonl'})
    first, second, legacy = llm._build_provider_specs()
    assert first.quality_log_file == str(tmp_path / 'quality' / 'ollama.jsonl')
    assert second.quality_log_file == str(tmp_path / 'quality' / 'second.jsonl')
    assert legacy.quality_log_file == ''
