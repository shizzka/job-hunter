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

## Package 5 — A7

Reproduction: all 3 original package tests failed. With a resume containing no
relocation consent, extraction emitted `willing_relocate=true`; an optimistic
verifier could certify “Готов к переезду” by citing those saved facts. Extractor
output containing its own `confirmed` section was also elevated to evidence.
Flat legacy facts had no confirmed provenance but were trusted.

Fix: the full model output is stored under `unconfirmed`, with model, timestamp,
source character count and resume SHA-256 provenance. Even an emitted
`confirmed` object stays inside `unconfirmed`. Extraction cannot erase existing
user-confirmed facts or bans, and CLI writes remain bound to the original path.
Both answer and cover grounding expose only explicit `confirmed` facts as
facts evidence; inferred, weak, extracted and flat legacy values cannot certify
personal claims. Direct resume evidence and confirmed interview facts still work.
Incomplete extraction is rejected.

Validation: 224 targeted facts/answer/cover/storage tests passed; another 69
package/grounding tests passed, including actual cover generation and preserving
user confirmations/bans. Browser validation is not applicable to extraction.
Limitation: old flat facts need explicit user confirmation before becoming
facts evidence. No production facts were read, migrated, or overwritten.

## Package 6 — A8

Reproduction: 6 failing / 2 passing actual Chromium tests before changes. Habr
and SuperJob both clicked old controls after `goto(B)` failed on vacancy A,
when navigation resolved to A, and on a foreign hostname carrying B's path.
Positive controls reached the expected controls on the correct destination.
The fixture explicitly declares UTF-8 so Habr's Cyrillic button selectors
actually match; no failure is hidden behind a missing-control result.

Fix: navigation exceptions stop immediately. A positively verified HTTPS source
hostname and exact vacancy ID are required before proceeding and again before
apply/submit actions after browser waits. SuperJob's supplied ID must also match
its approved URL. Habr uses the same canonical identity as its durable attempt.

Validation: 230 targeted native/parser/cookie/auth/A1 tests passed; 11 final
browser/identity tests passed, including destination changes during login await
and ID/URL mismatch before navigation. All browser requests were fulfilled with
synthetic HTML offline. No live native submit or production state was used.

## Local follow-ups — A9/F4/F5/F6

Reproduction: 6 semantic guard corruptions, 3 seen corruptions, 4 incomplete
cover responses, and 2 operational logging cases failed before fixes. The search
logging fixture initially used a wrong config name; after correcting that
fixture, the synthetic dispatch ran and both cover and questionnaire answer
sentinels appeared in captured logs. This fixture error is not counted as finding
evidence.

Fix: reuse ProtectedJsonStore with local schema validators for HH guard and seen.
Damaged bytes stay in place; repeated reads cannot reset cooldown/counts or make
old vacancies new, including after the nominal cooldown and explicit clear.
HH guard reports corruption as a current block; seen raises before application.
Cover generation requires finish_reason=stop before parsing or verification.
Affected search/chat/form/generation logs use IDs, counts, flags and exception
classes instead of cover text, answer dictionaries, message authors/bodies or
transport exception bodies. This is a scoped cleanup, not a global logging audit.

Validation: 191 targeted guard/seen/cover/search/chat/form tests passed.
Existing tests requiring automatic corrupt-state reset were strengthened to
require preservation and explicit restoration. Completed-response model fakes
now declare finish_reason=stop; incomplete-response regressions stay strict.

## Additional local boundary review — A1/A2/A3

Four added regressions failed after the primary packages: an outside submit
control could use approval of another form, a replaced form's submit event was
ignored by the capture barrier, late negative feedback erased a confirmed apply
outcome, and a generic status update could reopen an uncertain token.

Fix: bind the actual ElementHandle to the approved root before dispatch; capture
click validation applies to that exact control. Every submit event while armed
must belong to the original connected approved root. Preserve a confirmed
external outcome on late revocation; generic UI status changes cannot reopen or
erase external/uncertain attempts. Revocation before dispatch still blocks it.

Validation: 123 targeted HH/manual/browser tests passed, including all four new
regressions and the disabled/covered auto-wait races. This is an additional local
review, not the independent freeze-gate re-review required by the contract.

## Finding → reproduction → fix → regression → commit

All listed findings reproduced. Each primary package was committed only after
its regression and targeted checks passed, before work on the next package.
The additional boundary-review commit closes edge cases found afterward.

| Finding | Reproduction before fix | Code / resulting invariant | Regression tests | Commit |
| --- | --- | --- | --- | --- |
| A1 | HH/Habr retry after lost confirmation or cancellation; another run clicks again | `state_store/native_apply.py`, `hh/apply.py`, `habr_career_client.py`: durable acting/uncertain owner, one dispatch, no uncertain replay | `tests/test_audit_package1.py`: possible-click replay, post-click timeout, Habr response timeout/cancel, offline Chromium | `052e18f` |
| A3 | Two processes dispatch the same token; revocation during generation ignored | `manual_apply_queue.py`, `agent.py`: atomic owner claim, revocation checks, nonce-bound finalization | `tests/test_audit_package1.py`: two processes, revoked approval, cancellation, stale owner | `052e18f`; terminal-state review `c13d752` |
| A2 | Resume/letter/answers/fields change during disabled/covered click auto-wait | `hh/submit_boundary.py`, `hh/apply.py`, `hh/forms.py`: expected payload and actual control/root checked at browser events | `tests/test_audit_package2.py`: 10 Chromium races; `tests/test_audit_boundary_review.py`: outside control and replaced root | `5f76a16`; root/control review `c13d752` |
| A4 | Preview A regenerated as B; edited message/candidate/profile keeps old approval | `state_store/chat_responder.py`, `hh_chat_responder.py`, `telegram_bot_ui.py`: exact persistent text, hash/revision/input binding, no generation on approved send | `tests/test_audit_package3.py`: A/B, alternative revision, input drift, old callback, drift during fill | `26e6414` |
| A5 | Qualified yes/no becomes a platform shortcut | `hh/chat.py`: only plain full yes/no qualifies; all conditions remain in text | `tests/test_audit_package3.py`: semantic cases and actual offline Chromium sends | `26e6414` |
| A6 | Extra/missing selected options or failed text readback accepted; later fill mutates earlier field | `google_forms/filling.py`, `hh_chat_responder.py`: exact complete DOM readback after all fills, including optional approved fields | `tests/test_audit_package4.py`: stuck/unknown choices, readback failure, later mutations, exact positive controls | `fef2274` |
| A7 | Unstated relocation consent extracted then accepted as facts evidence; forged confirmed/flat legacy accepted | `facts.py`, `answer_grounding.py`, `matcher.py`, `agent.py`: extraction stays unconfirmed with provenance; only explicit confirmed facts enter evidence | `tests/test_audit_package5.py`: extraction grounding, forged confirmed, legacy, preserve confirmed/bans, actual cover verifier | `ba5c6e6` |
| A8 | Failed/wrong/foreign navigation clicks previous vacancy controls | `habr_career_client.py`, `superjob_client.py`: verified HTTPS source+ID after navigation and before actions | `tests/test_audit_package6.py`: offline Chromium failed/wrong/foreign/drifting/correct destination; ID/URL mismatch | `c5f9a12` |
| A9 | Valid JSON with invalid cooldown/date/timestamp types silently removes limits | `hh_guard.py`: semantic validation and repeated fail-closed block without modifying damaged bytes | `tests/test_audit_followups.py`: 6 corruptions, future read and clear | `8e868f5` |
| F4 | Broken seen file resets to empty; old vacancy becomes new | `seen.py`: protected validated history; reads/writes require restoration | `tests/test_audit_followups.py`: 3 corruptions, repeated read and write; strengthened `tests/test_seen.py` | `8e868f5` |
| F5 | Incomplete cover accepted by optimistic verifier | `matcher.py`: finish_reason=stop required before parsing/verifying | `tests/test_audit_followups.py`: length/content_filter/tool_calls/missing reason | `8e868f5` |
| F6 | Cover, answer dictionary, chat author/text in operational logs | `agent.py`, `matcher.py`, `hh/chat.py`, `hh/forms.py`, `hh_chat_responder.py`: scoped metadata-only logs | `tests/test_audit_followups.py`: synthetic private sentinels in search and chat verification | `8e868f5` |

## Remaining limitations

- Uncertain, interrupted and completed native attempts do not expire into retries.
  Human reconciliation and verified state restoration are required; no automatic
  reconciliation command or production-state migration is introduced.
- Habr's ambiguous initial apply control can open another form or submit directly.
  Automation records that boundary and stops if another action would be needed.
- Browser guards cover observable DOM state/events. They do not prove how a
  third-party backend interprets unchanged fields, and unsupported/dynamic UI can
  fail closed. No live source compatibility validation was performed.
- Flat legacy facts require explicit confirmation. Existing structured confirmed
  facts are treated as user evidence; this pass cannot reconstruct their history.
- Operational logging cleanup covers the demonstrated paths. Existing private
  journals and artifacts may contain candidate data under their existing controls.
- The contract's independent re-review remains outstanding. Phase 3 remains open,
  no release/tag/version bump is made, and Controlled Resume Tailoring is not begun.

## Full-suite fixture reconciliation

The first full run reported 7 failed / 2210 passed. Existing HH client doubles
recognized embedded identity/answer scripts inside the new arm script as old
standalone queries. They now model explicit arm/control/readback operations and
validate the expected ID, retained letter and answer plan, before interpreting
other scripts. The old two-step expectation also required a second potentially
external submit; it now asserts one click, retained letter, uncertain outcome and
no final submission. Production guards were unchanged. All 37 HH client tests
passed after reconciliation; the earlier combined browser/HH run passed its
other 62 cases.

## Final local validation

Source/tree commit: `218720e702d04d13b3861246392575ca67c66080`.
The final report commit adds documentation only.

- Full isolated offline suite: **2217 passed**, 162.39 seconds.
- Fresh `git archive HEAD` export, separate temporary HOME: **2217 passed**,
  166.04 seconds. No ignored/local-only files or production configuration included.
- `git diff --check` and `bash -n run.sh`: passed.
- Python test environment cleared with `env -i`; only interpreter PATH, locale,
  synthetic HOME and preinstalled Chromium path were supplied. Existing Python
  network-denial fixture remains enabled; browser cases use synthetic content
  and intercepted/aborted requests.
- GitHub workflow: `.github/workflows/offline-tests.yml` (existing offline-only
  workflow), dispatched on the final delivered branch HEAD after push. Exact run
  URL and outcome are reported in the delivery message once GitHub completes.

Local reproduction/validation logs are retained in `/tmp/jh-audit-evidence/`:
`package*-before.txt`, package targeted/final logs, `followups-before.txt`,
`followups-f6-before.txt`, `boundary-review-before.txt`, `full-suite.txt` (initial
fixture failures), `full-suite-final.txt`, and `export-suite-final.txt`.
Reproduction tests are committed for repeatable verification.

STOP after GitHub CI. No next phase, live action, production mutation, release
bump, or Phase 3 closure is authorized or performed by this pass.
