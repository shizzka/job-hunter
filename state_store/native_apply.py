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

    def reserve_recovery(self, url, owner):
        """Keep preparing claim-blocking while atomically forbidding dispatch."""
        def update(state):
            item = state.get(self.key(url), {})
            if item.get('owner') != owner or item.get('status') != 'preparing':
                raise RuntimeError('Native recovery owner/state changed')
            item['recovery_sealed'] = True
            return state
        self.store.update(update)

    def transition(self, url, owner, status, *, account='', resume_id=''):
        if status not in {'acting', 'completed', 'failed', 'uncertain'}:
            raise ValueError('Invalid native submit transition')
        def update(state):
            item = state.get(self.key(url), {})
            if item.get('owner') != owner:
                raise RuntimeError('Native submit owner changed')
            current = item.get('status')
            if current == 'uncertain' and status == 'uncertain':
                return state
            # Independent uncertainty can upgrade an owned terminal receipt;
            # it can never be downgraded to failed or completed.
            if current not in {'preparing', 'acting'} and not (status == 'uncertain' and current in {'failed', 'completed'}):
                raise RuntimeError('Native submit receipt is terminal')
            if item.get('recovery_sealed') and status in {'acting', 'completed'}:
                raise RuntimeError('Native submit owner was abandoned for recovery')
            if status == 'acting' and current != 'preparing':
                raise RuntimeError('Native submit already started')
            item.update(status=status, account=account, resume_id=resume_id)
            return state
        self.store.update(update)
        return True


class NativeAttempt:
    def __init__(self, repository, url, owner, approval=None, no_action=None, client=None):
        self.repository, self.url, self.owner = repository, url, owner
        self.approval = approval
        self.acting = False
        self.uncertain = False
        self.proven_no_action = False
        self.command_started = False
        self.abandoned = False
        self.no_action = no_action
        self.client = client
        self.page = getattr(client, '_page', None)
        self.context = getattr(client, '_context', None)

    def begin(self):
        binding = getattr(self.client, '_cookie_binding', None)
        if binding is not None and (binding.closing or binding.revoked):
            self.uncertain = True
            raise RuntimeError('Native cookie/browser ownership is closing or revoked')
        if self.client is not None and getattr(self.client, '_hh_recovery_stop_reason', ''):
            self.uncertain = True
            raise RuntimeError('Native browser was stopped before external action')
        if self.abandoned or (self.client is not None and getattr(self.client, '_recovering', False)):
            raise RuntimeError('Native attempt was abandoned before external action')
        if self.client is not None and (getattr(self.client, '_page', None) is not self.page
                or getattr(self.client, '_context', None) is not self.context):
            self.uncertain = True
            raise RuntimeError('Native browser ownership changed')
        monitor = getattr(self.page, '_hh_action_monitor', None)
        watch = getattr(self.context, '_hh_action_watch', None)
        if (monitor is not None and monitor.unknown) or (watch is not None and watch.unknown):
            self.uncertain = True
            raise RuntimeError('Native browser action evidence is uncertain')
        if self.acting:
            raise RuntimeError('External submit already attempted; reconcile manually')
        if self.approval is not None and not self.approval():
            raise RuntimeError('Manual approval revoked before external action')
        self.repository.transition(self.url, self.owner, 'acting')
        self.acting = True

    def seal_recovery(self):
        if self.acting or self.command_started or self.uncertain:
            self.uncertain = True
            raise RuntimeError('Recovery cannot discard a possible native action')
        self.abandoned = True
        try:
            self.repository.reserve_recovery(self.url, self.owner)
        except Exception:
            self.uncertain = True
            raise

    def confirm_no_action(self):
        """Only an owned browser receipt can prove the reserved command did not run."""
        if not self.acting:
            return
        if self.no_action is not None and not self.no_action():
            raise RuntimeError('Manual no-action receipt ownership lost')
        self.proven_no_action = True
        # Keep acting set: no second command is allowed within this attempt.


def _explicit_uncertain(result):
    return bool(result.get('uncertain')) or result.get('submission_status') in {'uncertain', 'preparing', 'acting'}


def _finish_receipt(attempt, status):
    """Ownership/read/write failures preserve uncertainty and never escape as failure."""
    try:
        state = attempt.repository.get(attempt.url)
        if state.get('owner') != attempt.owner:
            attempt.uncertain = True
            return 'uncertain'
        if state.get('status') == 'uncertain':
            attempt.uncertain = True
            return 'uncertain'
        if state.get('status') == 'acting' and not attempt.proven_no_action and status == 'failed':
            attempt.uncertain = True
            status = 'uncertain'
        if state.get('status') not in {'preparing', 'acting'}:
            attempt.uncertain = True
            status = 'uncertain'
        attempt.repository.transition(attempt.url, attempt.owner, status)
        return status
    except Exception:
        attempt.uncertain = True
        # A completion write may already be visible. Upgrade only this owner;
        # foreign owners and broken storage are left intact and fail closed.
        try:
            attempt.repository.transition(attempt.url, attempt.owner, 'uncertain')
        except Exception:
            pass
        return 'uncertain'


async def run_native_attempt(client, repository, url, operation):
    """Persist uncertainty before dispatch; cancellation cannot erase ownership."""
    page = getattr(client, '_page', None)
    monitor = getattr(page, '_hh_action_monitor', None)
    watch = getattr(getattr(client, '_context', None), '_hh_action_watch', None)
    binding = getattr(client, '_cookie_binding', None)
    if (getattr(client, '_external_attempt', None) is not None or getattr(client, '_recovering', False)
            or getattr(client, '_hh_recovery_stop_reason', '')
            or (monitor is not None and monitor.unknown) or (watch is not None and watch.unknown)
            or (binding is not None and (binding.closing or binding.revoked))):
        return {'ok': False, 'uncertain': True, 'reason': 'native_client_busy',
                'message': 'Browser уже принадлежит активной/остановленной попытке; нужна ручная сверка'}
    try:
        owner = repository.claim(url, '', '')
    except Exception:
        return {'ok': False, 'uncertain': True, 'reason': 'native_ownership_unproven',
                'message': 'Владелец попытки не подтверждён; нужна ручная сверка без автоматического повтора'}
    if not owner:
        return {'ok': False, 'uncertain': True, 'reason': 'native_attempt_not_retryable',
                'message': 'Существует активная/завершённая попытка; нужна ручная сверка'}
    attempt = NativeAttempt(repository, url, owner, getattr(client, '_manual_apply_guard', None),
                            getattr(client, '_manual_apply_no_action', None), client)
    client._external_attempt = attempt
    monitor = getattr(attempt.page, '_hh_action_monitor', None)
    try:
        if monitor is not None and repository.source == 'hh':
            # Retain every exact owned receipt before browser callbacks can
            # arrive, rather than replacing the only evidence on this Page.
            try:
                monitor.register(attempt)
            except Exception:
                attempt.uncertain = True
                raise
        result = await operation()
        monitor = getattr(attempt.page, '_hh_action_monitor', None)
        watch = getattr(attempt.context, '_hh_action_watch', None)
        if (monitor is not None and monitor.unknown) or (watch is not None and watch.unknown):
            attempt.uncertain = True
        if _explicit_uncertain(result):
            attempt.uncertain = True
        if attempt.proven_no_action and not attempt.uncertain:
            from hh.recovery import action_observed
            if action_observed(client, attempt):
                attempt.uncertain = True
            else:
                result = {**result, 'ok': False, 'uncertain': False}
        uncertain = attempt.uncertain or (attempt.acting and not attempt.proven_no_action and not result.get('ok'))
        status = _finish_receipt(attempt, 'uncertain' if uncertain else 'completed' if result.get('ok') else 'failed')
        if status == 'completed' and repository.source == 'hh':
            from hh.recovery import acknowledge_completed_action
            try:
                acknowledge_completed_action(client, attempt)
            except Exception:
                status = _finish_receipt(attempt, 'uncertain')
        if status == 'uncertain':
            result = {**result, 'ok': False, 'uncertain': True}
        return result
    except BaseException as exc:
        uncertain = attempt.uncertain or (attempt.acting and not attempt.proven_no_action)
        status = _finish_receipt(attempt, 'uncertain' if uncertain else 'failed')
        if status == 'uncertain' and isinstance(exc, Exception):
            if hasattr(exc, 'hh_recovered'):
                exc.hh_recovered = False
            return {'ok': False, 'uncertain': True, 'message': 'Результат отправки не подтверждён; нужна ручная сверка'}
        raise
    finally:
        if getattr(client, '_external_attempt', None) is attempt:
            client._external_attempt = None
