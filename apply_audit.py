"""Compact observations for auditing; never authorize a browser action."""

# These stages change the payload or re-run a gate that may await/rerender.
# QUESTIONS_FILLED is a post-questionnaire result summary, not a mutation.
INVALIDATES_VERIFICATION = frozenset({
    'WORK_CONDITIONS', 'COVER_LETTER', 'RESUME_SELECTED', 'RESUME_PREFLIGHT_STARTED',
    'VACANCY_NAVIGATION', 'VACANCY_READY', 'FORM_DETECTED', 'APPLY_CLICK',
})


def record_stage(stage, *, ok=None, **fields):
    """Persist only gate booleans/counts, even with optional debug tracing off."""
    if stage not in INVALIDATES_VERIFICATION | {'PRE_SUBMIT_VERIFY', 'SUBMIT_CLICK'}:
        return
    import analytics
    analytics.record_event({'event': 'application_safety', 'stage': stage, 'ok': ok,
        **{key: fields[key] for key in ('resume_match', 'letter_match', 'unanswered_required')
           if key in fields and type(fields[key]) in (bool, int)}})
