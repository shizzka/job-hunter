"""Short locked claims and version checks; never retain locks across awaits."""
import copy
import hashlib
import json
import re
import time
import uuid
from urllib.parse import urlparse

from google_forms import drafts
from google_forms.urls import _is_google_form_url
from state_store.google_forms import (
    GoogleFormStateRepository, MAX_PREVIEW_AGE_SECONDS, SUBMITTING_STATUS,
    TERMINAL_STATUSES, form_state_lock, valid_preview_state,
)


def revision(item, edits):
    # Operational result fields do not change the approved question/answer content.
    content = {k: v for k, v in item.items() if k not in {'status', 'submission', 'submit_result', 'submitted_at'}}
    encoded = json.dumps([content, edits], sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def answers_for(item, edits):
    answers = {int(a['index']): a for a in item.get('answers', [])}
    for q in item.get('questions', []):
        if drafts.question_key(q) in edits:
            answers[int(q['index'])] = edits[drafts.question_key(q)]
    return answers


def form_key(url):
    parsed = urlparse(url)
    # Ignore query prefill/fragment; resolved preview URLs are normally canonical.
    match = re.match(r'(/forms/d/(?:e/)?[^/]+)(?:/|$)', parsed.path)
    return (parsed.hostname or '').lower(), match[1] if match else parsed.path.rstrip('/')


class FormWorkflow:
    def __init__(self, home):
        self.home = home
        self.repository = GoogleFormStateRepository(home)

    def _capture_locked(self, token):
        item = drafts._get_draft_unlocked(self.home, token)
        edits = drafts.edits_store(self.home).load().get(token, {})
        return copy.deepcopy(item), copy.deepcopy(edits), revision(item, edits)

    def capture(self, token):
        drafts._validate_token(token)
        with form_state_lock(self.home):
            return self._capture_locked(token)

    def publish_recheck(self, token, expected_revision, detail):
        """Atomically publish successor and invalidate original approval in one file."""
        detail = copy.deepcopy(detail)
        new_token = detail.get('token')
        drafts._validate_token(new_token)
        if token == new_token or not valid_preview_state({'items': {new_token: detail}}):
            raise ValueError('Некорректная новая версия анкеты.')
        new_revision = revision(detail, {})
        with form_state_lock(self.home):
            original, _, current = self._capture_locked(token)
            if current != expected_revision:
                raise ValueError('Черновик изменён во время проверки. Откройте актуальную версию через /forms.')
            if not _is_google_form_url(detail.get('form_url')) or form_key(detail['form_url']) != form_key(original.get('form_url', '')):
                raise ValueError('Адрес формы изменился. Нужен новый preview и подтверждение.')
            def update(state):
                if new_token in state['items']:
                    raise ValueError('Новая версия уже существует; повторите проверку.')
                state['items'][new_token] = copy.deepcopy(detail)
                state['items'][token]['superseded_by'] = new_token
            self.repository._store.update(update)
            return new_revision

    def claim(self, token, *, expected_revision=None):
        drafts._validate_token(token)
        with form_state_lock(self.home):
            item, edits, current = self._capture_locked(token)
            if expected_revision is not None and current != expected_revision:
                raise ValueError('Черновик изменён. Нужна новая проверка и подтверждение.')
            if edits.get('answers'):
                raise ValueError('Черновик изменён. Нажмите «Проверить заполнение» и отправляйте новый preview.')
            answers = answers_for(item, {})
            if any(drafts.needs_review(q, answers.get(int(q['index']), {})) for q in item.get('questions', [])):
                raise ValueError('Нужны уточнения. Откройте черновик через /forms.')
            if item.get('status') != 'preview' or not item.get('questions') or not item.get('fill_result', {}).get('filled'):
                raise ValueError('google form preview is not ready for submit')
            if not _is_google_form_url(item.get('form_url')):
                raise ValueError('Unsafe Google Form URL; требуется новый preview.')
            if item.get('created_at') and time.time() - float(item['created_at']) > MAX_PREVIEW_AGE_SECONDS:
                raise ValueError('Черновик устарел; требуется новый preview.')
            attempt = uuid.uuid4().hex
            def update(state):
                for other_token, other in state['items'].items():
                    if (other_token != token and other.get('status') in TERMINAL_STATUSES | {SUBMITTING_STATUS}
                            and form_key(other.get('form_url', '')) == form_key(item['form_url'])):
                        raise ValueError('Эта форма уже отправлена или отправка требует ручной проверки.')
                owned = state['items'][token]
                owned['status'] = SUBMITTING_STATUS
                owned['submission'] = {'attempt_id': attempt, 'revision': current, 'phase': 'preparing',
                                       'started_at': int(time.time())}
            self.repository._store.update(update)
            return item, attempt

    def mark_submitting(self, token, attempt):
        """Durable ambiguity boundary immediately before the submit click."""
        with form_state_lock(self.home):
            edits = drafts.edits_store(self.home).load().get(token, {})
            def update(state):
                item = state['items'].get(token, {})
                claim = item.get('submission', {})
                if (item.get('status') != SUBMITTING_STATUS or claim.get('attempt_id') != attempt
                        or claim.get('phase') != 'preparing' or item.get('superseded_by')
                        or edits.get('superseded_by') or revision(item, edits) != claim.get('revision')):
                    raise ValueError('Версия или владелец отправки изменились. Нужна ручная проверка.')
                claim['phase'] = 'submitting'
            self.repository._store.update(update)
        return True

    def finish(self, token, attempt, result=None):
        """Update only this token/result; never replace pre-await whole state."""
        finished = False
        with form_state_lock(self.home):
            def update(state):
                nonlocal finished
                item = state['items'].get(token, {})
                claim = item.get('submission', {})
                if item.get('status') != SUBMITTING_STATUS or claim.get('attempt_id') != attempt:
                    return
                possible_click = claim['phase'] == 'submitting'
                detail = result or {'ok': False, 'token': token, 'submitted': possible_click,
                                    'message': 'Отправка прервана; проверьте результат вручную.'}
                item['status'] = ('submitted' if detail.get('ok') else 'submit_uncertain'
                                  if possible_click or detail.get('submitted') else 'submit_failed')
                item['submission']['phase'] = 'completed'
                item['submit_result'] = copy.deepcopy(detail)
                item['submitted_at'] = int(time.time())
                finished = True
            self.repository._store.update(update)
        return finished
