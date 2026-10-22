"""Compact observations for auditing; never authorize a browser action."""

# These stages change the payload or re-run a gate that may await/rerender.
# QUESTIONS_FILLED is a post-questionnaire result summary, not a mutation.
INVALIDATES_VERIFICATION = frozenset({
    'WORK_CONDITIONS', 'COVER_LETTER', 'RESUME_SELECTED', 'RESUME_PREFLIGHT_STARTED',
    'VACANCY_NAVIGATION', 'VACANCY_READY', 'FORM_DETECTED', 'APPLY_CLICK',
})
