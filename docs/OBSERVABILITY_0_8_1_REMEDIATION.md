# Scoped observability remediation: B1, NB1, NB2

Base: `d2cf02f2ff3286fde141012a10c0cab48426e725`.
Review: `docs/reviews/v0.8.1-observability-d2cf02f.md`, read from the separate
`job-hunter-observability-audit-report` checkout. The review is preserved.

Only the three requested findings are remediated. No search, matcher, apply,
chat, browser, approval, retry, provider, scheduler or production-runtime
behavior is changed. No version bump, merge, tag or deployment is included.

## B1 — Incomplete history falsely reports zero applications

Original finding: a successful application receipt survives in structured
analytics, but loss of the final history checkpoint leaves a startup zero which
Telegram/history present as a real zero. SIGKILL during cleanup reproduces the
same defect after `seen=applied` and a confirmed `application_result: sent`.

Reproducer: `tests/test_observability_remediation.py` includes the review's
minimal reader invariant, actual `agent.do_search` with actual apply-event
orchestration and a synthetic native client, failed/truncated final appends, and
SIGKILL of only an isolated child after the successful receipt/seen commit.
The native counter remains one. The child has an isolated HOME/state and IP
network is denied; no live browser or platform action is performed.

Result on the exact base: reproduced. Readers render zero; hard-kill and
lost/truncated-final regressions fail. Existing final-checkpoint tests did not
exercise this surviving-start-record condition.

Root cause: the startup checkpoint stores zero defaults, folding selects the
surviving checkpoint, and readers do not consult receipt events by run ID.

Fix:

- New incomplete checkpoints store unknown action counts as `null`, with
  additive `applied_count_status=unknown/minimum/exact` metadata. Positive
  observed counts are lower bounds, not promises about eventual totals.
- Read-only reconciliation extends the existing analytics journal reader. It
  uses the selected profile's event file and the same `run_id`. Confirmed
  `application_result: sent` and `decision: applied_auto` evidence is deduplicated
  by available application/vacancy identity. Identity-less receipts establish
  a minimum, without inventing distinct actions from duplicate unknown events.
- A correlated positive final analytics summary can also establish a minimum.
  An available finished summary with explicit integer `applied=0` and
  `apply_attempt=0` establishes zero dispatches. Missing/unreadable/truncated
  journals, attempts, errors, failures, uncertainty and already-applied outcomes
  do not establish a zero or a new application receipt.
- Missing finals remain incomplete. Application count is `>=N` if a minimum is
  known, otherwise `неизвестно`. CLI history, Telegram run/source summaries and
  the last-run stats row use the same formatter. Unknown incomplete counts do
  not generate a false zero-apply diagnosis. Explicit completed counts retain
  their existing presentation; missing legacy counts are unknown.
- Reconciliation neither rewrites history/journal nor touches protected action,
  seen, manual-queue, retry or browser state. There is no replay or new dispatch.

Regression: minimal receipt, unknown/unavailable journal, wrong run/profile,
unconfirmed outcomes, duplicate receipt/decision, raw startup checkpoint,
completed zero/positive counts, proof of no dispatch, missing run ID, missing
legacy count, dropped/truncated final append and actual SIGKILL tests.

## NB1 — Valid non-object JSON history record crashes CLI reader

Original finding/reproducer: an older `null` followed by six valid history
objects crashes `reporting.load_recent_run_history(5)` at `.get`.

Result on exact base: reproduced for null, string, arrays, integer, floating
number and boolean records, including records outside the recent window.

Root cause: folding assumes every parsed JSON value is an object.

Fix: the common folding reader ignores non-object records before using `.get`.
It preserves valid objects and folding/ordering, and never rewrites the file.
Regression checks the five recent valid records and unchanged file bytes for
all seven non-object shapes.

## NB2 — Notifier drops legacy keyword-stage source statistics

Original finding/reproducer:
`notifier._format_source_stats({'hh': {'relevant': 3}})` returns an empty string.

Result on exact base: reproduced for known and future/unknown sources.

Root cause: the notifier accepts only `keyword_pass`, unlike the other legacy
readers. Fix: fall back to `relevant` only when `keyword_pass` is absent, in both
source-formatting branches. The displayed label remains `keyword pass`;
explicit new-field zero takes precedence over a positive legacy alias.
Regression covers absence, precedence and explicit zero for both source types.

## Validation

Final regression file against an immutable base git archive: **22 failed,
6 passed** (4.90s). The three findings are reproduced; passing controls include
known final counts and new-field notifier precedence. Initial fixture-only
corrections are kept separately from this exact-base evidence.

Regression plus related observability, analytics, reporting and Telegram
suites: **141 passed** (6.95s). `git diff --check` and `bash -n run.sh` pass.

The complete offline suite is run from a clean git archive of the remediation
commit. Exact commit SHA, full result, archive checksum and push verification
are stored in `/home/q/job-hunter-observability-remediation-20261006-evidence`.
The user-facing closeout reports that exact pushed SHA and full-suite outcome;
this report does not claim release readiness or independent re-review approval.
