"""
Нормализованные outcome-статусы и decision-типы (D-002).

Единый источник правды для классификации решений и статусов переговоров.
"""

# ── Decision types (решение по вакансии) ──

DECISION_APPLIED_AUTO = "applied_auto"
DECISION_DRY_RUN_MATCH = "dry_run_match"
DECISION_SKIPPED_KEYWORD = "skipped_keyword_filter"
DECISION_SKIPPED_RED_FLAGS = "skipped_red_flags"
DECISION_SKIPPED_LOW_SCORE = "skipped_low_score"
DECISION_ALREADY_APPLIED = "already_applied"
DECISION_QUESTIONS_REQUIRED = "questions_required"
DECISION_APPLY_FAILED = "apply_failed"
DECISION_APPLY_FAILED_EXCEPTION = "apply_failed_exception"
DECISION_MANUAL_REVIEW = "manual_review"

# Группировка для аналитики
DECISIONS_AUTO_APPLIED = {DECISION_APPLIED_AUTO}
DECISIONS_MANUAL = {
    DECISION_QUESTIONS_REQUIRED,
    DECISION_APPLY_FAILED,
    DECISION_APPLY_FAILED_EXCEPTION,
    DECISION_MANUAL_REVIEW,
}
DECISIONS_FILTERED = {
    DECISION_SKIPPED_KEYWORD,
    DECISION_SKIPPED_RED_FLAGS,
    DECISION_SKIPPED_LOW_SCORE,
    DECISION_ALREADY_APPLIED,
}

# ── Negotiation status buckets ──

STATUS_POSITIVE = "positive"
STATUS_REJECTED = "rejected"
STATUS_PENDING = "pending"
STATUS_UNKNOWN = "unknown"

STATUS_DETAIL_INTERVIEW = "interview"
STATUS_DETAIL_OFFER = "offer"
STATUS_DETAIL_TEST_TASK = "test_task"
STATUS_DETAIL_POSITIVE_OTHER = "positive_other"
STATUS_DETAIL_REJECTED = "rejected"
STATUS_DETAIL_PENDING_VIEWED = "pending_viewed"
STATUS_DETAIL_PENDING_NEW = "pending_new"
STATUS_DETAIL_UNKNOWN = "unknown"

# Ключевые слова для классификации статусов hh.ru
_POSITIVE_TOKENS = (
    "приглаш", "собесед", "оффер", "выход на работу",
    "тестовое задание", "предложение",
)
_REJECTED_TOKENS = ("отказ", "отклонен")
_PENDING_TOKENS = ("не просмотрен", "просмотрен", "ожидание")
_INTERVIEW_TOKENS = ("собесед",)
_OFFER_TOKENS = ("оффер", "выход на работу", "приглашение на работу", "предложение")
_TEST_TASK_TOKENS = ("тестов",)
_PENDING_NEW_TOKENS = ("не просмотрен",)
_PENDING_VIEWED_TOKENS = ("просмотрен", "ожидание")


def status_bucket(status_text: str) -> str:
    """Классифицировать текстовый статус переговоров в bucket."""
    text = (status_text or "").strip().casefold()
    if not text:
        return STATUS_UNKNOWN
    if any(token in text for token in _POSITIVE_TOKENS):
        return STATUS_POSITIVE
    if any(token in text for token in _REJECTED_TOKENS):
        return STATUS_REJECTED
    if any(token in text for token in _PENDING_TOKENS):
        return STATUS_PENDING
    return STATUS_UNKNOWN


def status_detail_bucket(status_text: str) -> str:
    """Более детальная классификация статусов переговоров."""
    text = (status_text or "").strip().casefold()
    if not text:
        return STATUS_DETAIL_UNKNOWN
    if any(token in text for token in _INTERVIEW_TOKENS):
        return STATUS_DETAIL_INTERVIEW
    if any(token in text for token in _OFFER_TOKENS):
        return STATUS_DETAIL_OFFER
    if any(token in text for token in _TEST_TASK_TOKENS):
        return STATUS_DETAIL_TEST_TASK
    if any(token in text for token in _REJECTED_TOKENS):
        return STATUS_DETAIL_REJECTED
    if any(token in text for token in _PENDING_NEW_TOKENS):
        return STATUS_DETAIL_PENDING_NEW
    if any(token in text for token in _PENDING_VIEWED_TOKENS):
        return STATUS_DETAIL_PENDING_VIEWED
    if status_bucket(text) == STATUS_POSITIVE:
        return STATUS_DETAIL_POSITIVE_OTHER
    return STATUS_DETAIL_UNKNOWN
