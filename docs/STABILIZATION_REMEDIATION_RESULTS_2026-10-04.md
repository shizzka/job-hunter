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
