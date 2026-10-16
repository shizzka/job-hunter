"""Public chat reply outcomes shared by the CLI and Telegram controller."""
import json


_FACT_REASONS = {
    'unsupported_claims', 'nonliteral_quote', 'unknown_source', 'missing_evidence',
    'number_mismatch',
}
_CHECK_REASONS = {'invalid_check', 'incomplete_check', 'invalid_evidence', 'invalid_grounding_check'}
_MESSAGES = {
    'model_unavailable': 'модель недоступна. Попробуйте ещё раз позже',
    'grounding_unavailable': 'модель проверки фактов недоступна. Попробуйте ещё раз позже',
    'generation_incomplete': 'модель вернула неполный ответ',
    'grounding_incomplete': 'проверка фактов не завершилась',
    'invalid_grounding_check': 'модель вернула некорректную проверку фактов',
    'invalid_grounding_input': 'недостаточно данных профиля для проверки фактов',
    'invalid_model_answer': 'модель вернула некорректный ответ',
    'needs_profile_details': 'в профиле недостаточно данных для ответа',
    'empty_answer': 'модель не подготовила текст ответа',
    'unrelated_answer': 'модель ответила не на заданный вопрос',
    'missing_question': 'вопрос не найден или чат недоступен',
    'stale_question': 'вопрос изменился. Откройте свежую карточку вопроса',
    'stale_draft': 'черновик изменился или устарел. Подготовьте новый вариант',
    'question_not_approved': 'для этого сообщения требуется подтверждение владельца',
    'attempt_blocked': 'попытка заблокирована. Проверьте состояние чата',
    'chat_unavailable': 'чат недоступен для подготовки и отправки ответа',
    'preview_too_long': 'черновик слишком длинный для показа в Telegram',
    'send_uncertain': 'отправка не подтверждена. Проверьте чат перед повторной отправкой',
    'preparation_error': 'ошибка подготовки ответа. Подробности в журнале',
}
_RETRYABLE = {
    'model_unavailable', 'grounding_unavailable', 'generation_incomplete',
    'grounding_incomplete', 'invalid_grounding_check', 'invalid_model_answer',
    'empty_answer', 'unrelated_answer', 'preview_too_long',
} | _FACT_REASONS | _CHECK_REASONS


def reply_result(detail):
    """Only metadata crosses stdout; candidate text stays in the private preview."""
    reason = detail.get('reason', 'sent' if detail.get('sent') else 'drafted' if detail.get('ok')
                        else 'preparation_error')
    return {key: detail[key] for key in
            ('ok', 'reason', 'dry_run', 'sent', 'already_replied', 'notification_uncertain')
            if key in detail} | {'reason': reason, 'retryable': reason in _RETRYABLE}


def parse_reply_result(stdout):
    for line in reversed((stdout or '').splitlines()):
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and isinstance(value.get('chat_reply_result'), dict):
            return value['chat_reply_result']
    # Honest fallback for an older child process during upgrades.
    if 'OK: False' in (stdout or ''):
        return {'ok': False, 'reason': 'preparation_error', 'retryable': False}
    return None


def reply_failure_text(reason):
    message = ('ответ не подтверждён фактами профиля' if reason in _FACT_REASONS else
               'модель вернула некорректную проверку фактов' if reason in _CHECK_REASONS else
               _MESSAGES.get(reason, 'проверка ответа не пройдена'))
    return f'Не удалось подготовить ответ: {message}.'
