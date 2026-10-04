"""Durable per-vacancy GeekJob submission owners; no automatic uncertain replay."""
import hashlib
import math
from pathlib import Path
import re
import time
import uuid
from urllib.parse import unquote, urlsplit

from .protected import ProtectedJsonStore


def _valid(state):
    for key, item in state.items():
        if (not re.fullmatch(r'[0-9a-f]{64}', key) or not isinstance(item, dict)
                or item.get('status') not in {'preparing', 'acting', 'completed', 'failed', 'uncertain'}
                or not re.fullmatch(r'[0-9a-f]{32}', str(item.get('owner', '')))
                or any(not isinstance(item.get(field), str) for field in ('session', 'approval'))
                or type(item.get('started_at')) not in (int, float)
                or not math.isfinite(item['started_at'])):
            return False
    return True


class GeekJobApplyRepository:
    def __init__(self, cookies_file):
        self.path = Path(cookies_file).absolute().with_name('geekjob_apply_attempts.json')
        self.store = ProtectedJsonStore(self.path, default_factory=dict, validator=_valid)

    @staticmethod
    def key(url):
        return hashlib.sha256(canonical_vacancy_url(url).encode()).hexdigest()

    def get(self, url):
        return self.store.load().get(self.key(url), {})

    def claim(self, url, session, approval):
        key, owner = self.key(url), uuid.uuid4().hex
        claimed = False
        def update(state):
            nonlocal claimed
            previous = state.get(key, {})
            if previous.get('status') in {'preparing', 'acting', 'completed', 'uncertain'}:
                return state
            state[key] = {'owner': owner, 'status': 'preparing', 'session': session,
                          'approval': approval, 'started_at': time.time()}
            claimed = True
            return state
        self.store.update(update)
        return owner if claimed else None

    def transition(self, url, owner, status, *, account='', resume_id=''):
        if status not in {'acting', 'completed', 'failed', 'uncertain'}:
            raise ValueError('Invalid GeekJob submit transition')
        changed = False
        def update(state):
            nonlocal changed
            item = state.get(self.key(url), {})
            if item.get('owner') != owner or item.get('status') not in {'preparing', 'acting'}:
                raise RuntimeError('GeekJob submit owner changed')
            if status == 'acting' and item['status'] != 'preparing':
                raise RuntimeError('GeekJob POST already started')
            item.update(status=status, account=account, resume_id=resume_id)
            changed = True
            return state
        self.store.update(update)
        return changed


def canonical_vacancy_url(url):
    parsed = urlsplit(url)
    path = unquote(parsed.path).rstrip('/')
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)
            or not re.fullmatch(r'/vacancy/[A-Za-z0-9_-]+', path)):
        raise ValueError('Invalid GeekJob vacancy URL identity')
    prefix, external_id = path.rsplit('/', 1)
    if re.fullmatch(r'[0-9a-fA-F]{24}', external_id):
        external_id = external_id.casefold()
    return f'https://{parsed.hostname.casefold()}{prefix}/{external_id}'
