"""HH/Habr attempt records using the existing GeekJob owner semantics."""
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

from .geekjob_apply import GeekJobApplyRepository, _valid
from .protected import ProtectedJsonStore


class NativeApplyRepository(GeekJobApplyRepository):
    def __init__(self, cookies_file, source):
        if source not in {'hh', 'habr'}:
            raise ValueError('Unsupported native apply source')
        self.source = source
        self.path = Path(cookies_file).absolute().with_name(source + '_apply_attempts.json')
        self.store = ProtectedJsonStore(self.path, default_factory=dict, validator=_valid)

    def key(self, url):
        parsed = urlsplit(url)
        host = (parsed.hostname or '').casefold()
        pattern = r'/vacancy/(\d+)/?' if self.source == 'hh' else r'/vacancies/(\d+)/?'
        match = re.fullmatch(pattern, parsed.path)
        host_valid = (host == 'hh.ru' or host.endswith('.hh.ru')) if self.source == 'hh' else host == 'career.habr.com'
        if (not match or not host_valid or parsed.scheme != 'https' or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)):
            raise ValueError('Unverified native vacancy identity')
        return hashlib.sha256((self.source + ':' + match[1]).encode()).hexdigest()


class NativeAttempt:
    def __init__(self, repository, url, owner, approval=None):
        self.repository, self.url, self.owner = repository, url, owner
        self.approval = approval
        self.acting = False

    def begin(self):
        if self.acting:
            raise RuntimeError('External submit already attempted; reconcile manually')
        if self.approval is not None and not self.approval():
            raise RuntimeError('Manual approval revoked before external action')
        self.repository.transition(self.url, self.owner, 'acting')
        self.acting = True


async def run_native_attempt(client, repository, url, operation):
    """Persist uncertainty before dispatch; cancellation cannot erase ownership."""
    if getattr(client, '_external_attempt', None) is not None:
        return {'ok': False, 'uncertain': True, 'reason': 'native_client_busy',
                'message': 'Browser уже принадлежит активной попытке'}
    owner = repository.claim(url, '', '')
    if not owner:
        return {'ok': False, 'uncertain': True, 'reason': 'native_attempt_not_retryable',
                'message': 'Существует активная/завершённая попытка; нужна ручная сверка'}
    attempt = NativeAttempt(repository, url, owner, getattr(client, '_manual_apply_guard', None))
    client._external_attempt = attempt
    try:
        result = await operation()
        status = 'completed' if result.get('ok') else ('uncertain' if attempt.acting else 'failed')
        repository.transition(url, owner, status)
        if status == 'uncertain':
            result = {**result, 'ok': False, 'uncertain': True}
        return result
    except BaseException as exc:
        repository.transition(url, owner, 'uncertain' if attempt.acting else 'failed')
        if attempt.acting and isinstance(exc, Exception):
            return {'ok': False, 'uncertain': True, 'message': 'Результат отправки не подтверждён; нужна ручная сверка'}
        raise
    finally:
        if getattr(client, '_external_attempt', None) is attempt:
            client._external_attempt = None
