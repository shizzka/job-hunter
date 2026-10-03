"""Private opt-in request/response traces for the temporary Ollama text path."""
from datetime import datetime, timezone
import logging
from pathlib import Path
import re

from debug_trace import _redact_text
from state_store.private_journal import append_json

log = logging.getLogger(__name__)
_PRIVATE_KEYS = {'api_key', 'authorization', 'cookie', 'cookies', 'password', 'secret',
                 'access_token', 'refresh_token', 'client_secret', 'headers', 'extra_headers'}
_REQUEST_FIELDS = {'model', 'messages', 'temperature', 'max_tokens', 'max_completion_tokens',
                   'response_format', 'reasoning_effort', 'top_p', 'stop', 'seed', 'stream'}


def _scrub(value):
    # Preserve full prompts/answers for quality review, but not transport secrets.
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if str(k).casefold() in _PRIVATE_KEYS else _scrub(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return re.sub(r'\b(?:gsk_|hf_|gh[pousr]_)[A-Za-z0-9_-]{20,}', '[REDACTED]',
                      _redact_text(value, limit=None))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError('Unsupported quality trace value')


def record(provider, *, event, call_id, requested_model, actual_model,
           request=None, response=None, error=None, latency_seconds=None):
    """Best-effort private append; quality telemetry must never change LLM results.

    Contains candidate personal data by explicit opt-in. No stdout/raw error text,
    HTTP headers, base URL or API key. Streaming chunks are not consumed here.
    """
    if not provider.text_fallback_model or not provider.quality_log_file:
        return
    try:
        row = {'schema_version': 1, 'event': event, 'call_id': call_id,
               'created_at': datetime.now(timezone.utc).isoformat(),
               'provider': provider.name, 'requested_model': requested_model,
               'actual_model': actual_model}
        if latency_seconds is not None:
            row['latency_seconds'] = round(latency_seconds, 6)
        if request is not None:
            row['request'] = {key: value for key, value in request.items() if key in _REQUEST_FIELDS}
        if response is not None:
            dump = getattr(response, 'model_dump', None)
            if callable(dump):
                row['response'] = dump(mode='json')
            else:
                row['response'] = {'model': getattr(response, 'model', actual_model), 'choices': [
                    {'finish_reason': getattr(choice, 'finish_reason', None),
                     'message': {'content': getattr(getattr(choice, 'message', None), 'content', None)}}
                    for choice in getattr(response, 'choices', [])]}
        if error is not None:
            row['error_kind'] = type(error).__name__
            status = getattr(error, 'status_code', None)
            if isinstance(status, int):
                row['status_code'] = status
        if request is not None and request.get('stream'):
            row['stream_chunks_logged'] = False
        path = Path(provider.quality_log_file).expanduser().absolute()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        append_json(path, _scrub(row))
    except Exception as exc:
        log.warning('Ollama quality trace unavailable (%s)', type(exc).__name__)
