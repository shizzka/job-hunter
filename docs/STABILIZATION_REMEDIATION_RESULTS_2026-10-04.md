# Stabilization remediation results — 2026-10-04

Scope: contract `STABILIZATION_AUDIT_REMEDIATION_2026-10-04.md`, branch
`audit-remediation-2026-10-04`. All actions in tests use fakes or offline Chromium;
state is synthetic and temporary. No production state, applications, Telegram
sends, or external submissions were used. Phase 3 remains open; no v0.8.0 bump.

## Package 1 — A1/A3

Before behavior changes, `tests/test_audit_package1.py` failed in 11 cases:
HH repeated click after post-click timeout, HH retried via DOM after lost
confirmation, Habr retried after response timeout, cancellation left attempts
retryable, two forked processes dispatched the same token twice, and negative
feedback during generation did not block dispatch. The owner-finalization test
also demonstrated that no owner-bound claim API existed.

Fix: HH/Habr reuse the protected JSON / GeekJob attempt-owner semantics, mark
`acting` durably before dispatch, persist ambiguous outcomes as `uncertain`, and
reject subsequent automatic attempts. HH DOM retry and Habr confirmation retry
are removed. Post-action inspection cannot invoke another click strategy.
Manual approval now atomically claims `pending -> applying` with a nonce, checks
revocation again after awaits and at the action boundary, and finalizes only with
that nonce. Active/uncertain tokens are retained during queue pruning.

Validation: 192 targeted HH/manual/UI/resume/form tests passed; another 31
package/source-cookie tests passed, including actual offline Chromium proving
one click across a lost confirmation and a subsequent attempt.
The old closing test required `dom_retry`; its expectation now requires zero
repeat submit, preserving all identity/letter/questionnaire guards.

Limitations: ambiguous and interrupted attempts require explicit human
reconciliation; there is no automatic expiry/replay. The Habr initial control may
itself send a quick response. If it opens a further form, automation stops for
manual review instead of issuing another action. A form already open can submit
once. Completed native attempts also remain non-retryable automatically.

## Package 2 — A2

Reproduction: real Chromium with disabled/covered submit controls, a passing final
identity guard, then changes during Playwright's actual `element.click()` wait.
Before the fix: 8 failures / 2 passing unchanged controls. Resume, letter,
questionnaire answer, and added-field changes all reached the synthetic submit
handler. Every browser request was aborted.

Fix: a capture-phase browser barrier binds the exact expected resume ID and
cover letter, the questionnaire answer plan, and a snapshot of all form fields.
It checks them synchronously at both click and submit events, before page
handlers, including after Playwright auto-wait. Root replacement and payload
changes block the event. Existing unexpected-UI capture guards remain active.
Questionnaire readback and resume selection use the same JS classifiers as the
boundary. An active client cannot be reused concurrently to overwrite its
approved payload.

Validation: all 10 browser race scenarios passed; 198 combined HH/A1/A2,
questionnaire, resume picker and modal safety tests passed.
A browser-blocked action is conservatively uncertain if its command was already
dispatched; it is never automatically replayed.

## Package 3 — A4/A5

Reproduction: 8 failing / 3 passing package tests before changes. A shown as the
preview was replaced by generated B at send; same-ID message edits and changed
candidate/profile data did not invalidate approval. Qualified yes/no replies
were shortened to a platform shortcut. No draft revision was persisted.

Fix: exact draft text, SHA-256, random revision, message/history/vacancy revision,
and candidate/profile snapshot hash live in the existing protected chat state.
A send button carries `message_id~revision` through the existing Telegram/CLI
route. Approved sends read stored text without generation and recheck inputs
before the durable action boundary. New/alternative generation invalidates the
old revision as it starts. Legacy unbound send buttons are rejected. Preview
shows full outgoing text; oversized notifications fail closed instead of
allowing Telegram truncation with a send button. Existing autosend/direct
one-shot generation without a previous preview retains its existing behavior;
it cannot use an existing same-message preview without its revision.

Quick replies now require a plain full yes/no, with optional terminal punctuation.
Any qualifiers use the full text input. Validation: 182 targeted chat/state/
Telegram/Chromium tests passed; actual offline Chromium verifies exact qualified
text delivery and the plain-yes shortcut. No live chat or Telegram send occurred.

## Package 4 — A6

Reproduction: 7 failing / 2 passing offline tests before changes. Extra checkbox
selection survived attempted clearing; a missing/unavailable desired option or
unknown approved label was accepted as a subset; failed text readback substituted
the expected string. Chromium demonstrated that filling a later field could
change a previously accepted checkbox without preventing submit readiness.

Fix: require exact approved option labels and the complete selection set, never
substitute expected values on readback errors, and re-fetch/read every question
after all fills. Optional approved fields also block readiness on any mismatch.
Skipped fields must actually be empty. Unknown states, duplicate labels, or
changed DOM item count fail closed. No changes to Forms claim, version ownership,
or durable `submitting -> submit_uncertain` handling.

Validation: 178 targeted Forms/workflow/state tests passed; final exact-option
checks passed in another 15 package/filling tests. Chromium covered extra,
missing, mutated text, and exact-success states, with all requests aborted.
