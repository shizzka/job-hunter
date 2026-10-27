"""Read pending decisions from existing workflows; postpone without consuming them."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import time
import uuid

import chat_context
import profile_access
import question_memory
from google_forms import drafts
from state_store.chat_responder import ChatResponderStateRepository
from state_store.google_forms import GoogleFormStateRepository
from state_store.protected import ProtectedJsonStore


def oldest_key(item):
    value = item.get('created_ts') or item.get('created_at') or 0
    try:
        return float(value)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except ValueError:
            return 0


def review_vacancies(view, items, *, unspecified=False):
    bound = profile_access.current_scope()[1]
    if bound or unspecified:
        return view.review_items(items, unspecified=unspecified)
    return [item for item in items if view.item_direction(item) is not None]


def chat_allowed(view, chat_id):
    bound = profile_access.current_scope()[1]
    if bound:
        owner = chat_context.safe_resolve(view.home, str(chat_id or ''))
        return owner.status == chat_context.OWN and owner.direction == bound
    if view.chat_status(chat_id) == 'selected':
        return True
    return chat_context.safe_resolve(view.home, str(chat_id or '')).status == chat_context.OWN


def _valid_postponements(state):
    return isinstance(state, dict) and all(isinstance(key, str) and
        type(value) in (int, float) and math.isfinite(value) for key, value in state.items())


class InboxPostponements:
    def __init__(self, home):
        self.store = ProtectedJsonStore(Path(home) / 'pending_inbox_postponements.json',
                                        validator=_valid_postponements)

    def load(self):
        return self.store.load()

    @staticmethod
    def version(deadline=0):
        return hashlib.sha256(repr(float(deadline)).encode()).hexdigest()[:8]

    def postpone(self, token, *, expected_version, now=None):
        now = time.time() if now is None else now
        changed = False
        def update(state):
            nonlocal changed
            if (self.version(state.get(token, 0)) == expected_version
                    and state.get(token, 0) <= now):
                state[token] = now + 86400
                changed = True
        self.store.update(update)
        return changed


def pending_workflows(view):
    """Only usable owned forms and the current, unsent revision of each chat draft."""
    home = view.home
    postponed = InboxPostponements(home).load()
    now = time.time()
    result, seen = [], set()
    edits = drafts.edits_store(home).load()
    forms = GoogleFormStateRepository(home).load()['items']
    for token, item in sorted(forms.items(), key=lambda entry: oldest_key(entry[1]), reverse=True):
        if item.get('superseded_by') or edits.get(token, {}).get('superseded_by'):
            continue
        key = (item.get('chat_id'), str(item.get('form_url', '')).split('?')[0])
        if key in seen:
            continue
        seen.add(key)
        if (not item.get('questions') or item.get('status') in drafts.TERMINAL | {'submit_in_progress'}
                or not chat_allowed(view, item.get('chat_id'))):
            continue
        inbox_token = 'f' + token
        if postponed.get(inbox_token, 0) > now:
            continue
        result.append(dict(item, token=inbox_token, form_token=token, _inbox_kind='form',
                           postpone_version=InboxPostponements.version(postponed.get(inbox_token, 0))))
    for chat_id, chat in ChatResponderStateRepository(home).load().items():
        draft = chat.get('draft')
        if (not draft or chat.get('last_replied_msg_id') == draft['message_id']
                or any(attempt.get('status') in {'preparing', 'acting'}
                       or attempt.get('kind') == 'reply' and attempt.get('status') == 'uncertain'
                       for attempt in chat.get('attempts', {}).values())
                or not chat_allowed(view, chat_id)):
            continue
        token = 'd' + hashlib.sha256(f"{chat_id}|{draft['revision']}".encode()).hexdigest()[:12]
        if postponed.get(token, 0) > now:
            continue
        result.append({'token': token, '_inbox_kind': 'draft', 'chat_id': chat_id,
                       'draft': draft, 'created_ts': chat.get('last_previewed_at', 0),
                       'postpone_version': InboxPostponements.version(postponed.get(token, 0))})
    return result



def native_questions(view, vacancies, profile):
    """A live question replaces its linked pending vacancy, including while postponed."""
    linked = {item['token']: item for item in vacancies if item.get('status') == 'pending'}
    postponed = InboxPostponements(view.home).load()
    items, replaced = [], set()
    saved = question_memory._read(str(Path(view.home) / question_memory.PENDING_FILE))
    now = time.time()
    for qid, question in sorted(saved.items(), key=lambda entry: oldest_key(entry[1])
                                if isinstance(entry[1], dict) else 0, reverse=True):
        if not isinstance(question, dict) or question.get('profile_name') != profile:
            continue
        try:
            created = float(question.get('created_at', 0))
        except (ValueError, TypeError):
            continue
        if not math.isfinite(created) or now - created >= question_memory.PENDING_TTL_SECONDS:
            continue
        candidate = linked.get(question.get('token'))
        if not candidate or candidate['token'] in replaced:
            continue
        direction = view.item_direction(candidate)
        if question.get('direction', '') != direction:
            continue
        vacancy, evaluation = candidate.get('vacancy') or {}, candidate.get('evaluation') or {}
        selection = vacancy.get('_direction_selection') or {}
        resume_ids = {str(source.get(key)) for source in (selection, vacancy, evaluation)
                      for key in ('hh_resume_hash', 'hh_resume_numeric_id') if source.get(key)}
        resume = question.get('resume_id') or ''
        if resume and str(resume) not in resume_ids:
            continue
        token = 'q' + hashlib.sha256(json.dumps([str(qid), question], sort_keys=True,
                                                    ensure_ascii=False).encode()).hexdigest()[:12]
        replaced.add(candidate['token'])
        if postponed.get(token, 0) > now:
            continue
        items.append(dict(question, token=token, qid=str(qid), vacancy=vacancy,
                          _inbox_kind='question',
                          postpone_version=InboxPostponements.version(postponed.get(token, 0))))
    return items, replaced


def question_markup(item, profile):
    rows = []
    if item.get('control') in {'radio', 'checkbox', 'select'}:
        for index, label in enumerate(item.get('options') or []):
            rows.append([{'text': str(label)[:60], 'callback_data': f"rq:Q:{profile}:{item['token']}:{index}"}])
    rows.append([{'text': '✏️ Ответить своими словами', 'callback_data': f"rq:T:{profile}:{item['token']}"}])
    rows.append([{'text': '◀ Назад', 'callback_data': f"rq:c:{profile}:{item['token']}"}])
    if any(len(button['callback_data'].encode()) > 64 for row in rows for button in row):
        raise ValueError('Слишком длинная ссылка на вопрос.')
    return {'inline_keyboard': rows}

def card_text(item, *, position, total):
    kind = item['_inbox_kind']
    label = '📝 Анкеты' if kind in {'form', 'question'} else '💬 Ответ работодателю'
    title = (item.get('vacancy') or {}).get('title')
    lines = [f'📋 Ждут решения · {position} из {total}', '', label]
    if title:
        lines.append(str(title))
    if kind == 'question':
        lines.extend(['', str(item.get('question') or ''), '',
                      'Ваш ответ сохранится. После ответа бот повторит отклик на эту вакансию.'])
    elif kind == 'form':
        lines.extend(['', f"Полей: {len(item.get('questions') or [])}",
                      'Проверьте ответы в анкете. Отправка — после вашего подтверждения.'])
    else:
        lines.extend(['', str(item['draft']['answer']), '',
                      'Без вашего «Отправить» ответ не отправится.'])
    return '\n'.join(lines)


def card_markup(item, profile):
    def button(text, action):
        data = f"rq:{action}:{profile}:{item['token']}"
        if action == 'P':
            data += ':' + item['postpone_version']
        if len(data.encode()) > 64:
            raise ValueError('Слишком длинная ссылка на решение.')
        return {'text': text, 'callback_data': data}
    rows = ([[button('📝 Заполнить', 'F')]] if item['_inbox_kind'] in {'form', 'question'} else
            [[button('✅ Отправить', 'S')], [button('✏️ Изменить', 'E')]])
    rows.extend([[button('⏭ Позже', 'P')], [button('◀', 'h'), button('▶', 'v')],
                 [{'text': '☰ Вся очередь', 'callback_data': f'rq:l:{profile}:0'}]])
    return {'inline_keyboard': rows}


def edit_chat_draft(home, chat_id, revision, answer):
    """Compare and replace only an idle draft; sending retains the workflow guards."""
    changed = None
    def update(chat):
        nonlocal changed
        draft = chat.get('draft')
        if (not draft or draft['revision'] != revision
                or chat.get('last_replied_msg_id') == draft['message_id']
                or any(attempt.get('status') in {'preparing', 'acting'}
                       or attempt.get('kind') == 'reply' and attempt.get('status') == 'uncertain'
                       for attempt in chat.get('attempts', {}).values())):
            raise ValueError('Черновик изменён или уже отправляется. Откройте очередь заново.')
        changed = dict(draft, answer=answer, answer_hash=hashlib.sha256(answer.encode()).hexdigest(),
                       revision=uuid.uuid4().hex[:12], source='telegram_manual')
        chat['draft'] = changed
    ChatResponderStateRepository(home).update_chat(chat_id, update)
    return changed
