"""Versions of local resume inputs, not assertions about remote HH resume contents."""
import hashlib


def record_input(vacancy, text, stage):
    if stage not in {'matcher', 'cover_letter'}:
        raise ValueError('Unknown resume input stage')
    versions = dict(vacancy.get('_resume_input_versions') or {})
    # Missing-file placeholder must not masquerade as a real resume version.
    missing = not text.strip() or text.startswith('(Резюме не найдено')
    versions[stage] = {
        'sha256': None if missing else hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'status': 'missing' if missing else 'available',
        'scope': 'local_llm_input',
    }
    vacancy['_resume_input_versions'] = versions


def payload(vacancy):
    versions = vacancy.get('_resume_input_versions') or {}
    result = {}
    for stage in ('matcher', 'cover_letter'):
        value = versions.get(stage) or {}
        result[f'{stage}_resume_sha256'] = value.get('sha256')
        result[f'{stage}_resume_status'] = value.get('status', 'unknown')
    result['resume_version_scope'] = 'local_llm_input'
    requested = vacancy.get('_requested_resume') or {}
    result['requested_resume_id'] = requested.get('id', '')
    result['requested_resume_title'] = requested.get('title', '')
    return result
