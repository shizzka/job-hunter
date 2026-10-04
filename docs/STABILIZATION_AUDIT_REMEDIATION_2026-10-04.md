# Stabilization audit remediation — 2026-10-04

## Purpose

This document is the implementation contract for the post-audit stabilization pass.

Baseline audited by the independent reviewer:

- main: `e59ac2f61a2f89bb374bebf6b101214031008313`
- last production-code stabilization commit: `f6394c2a70f83551d0bb6f02184eba1b2faeeb58`
- audit verdict: **NOT READY**
- freeze blockers: A1–A8 below

The goal is **not** to redesign Job Hunter. The goal is to close the confirmed unsafe
behavior with local changes, regression tests, and a short re-review.

Do not use documentation claims as proof that a finding is fixed. Reproduce or
otherwise prove the invariant in code/tests.

## Hard constraints

- No real applications.
- No real Telegram sends.
- No external submit.
- No production-state mutation.
- No new Gateway/framework/database/service.
- No broad refactor unless a local fix is demonstrably impossible.
- Preserve existing fail-closed behavior.
- Keep provider/model architecture unchanged.
- Do not implement findings from older audits that are already disproven on current main.
- Never weaken an existing guard merely to make a test pass.

## Workflow

For each package below:

1. Reproduce the finding against the current branch before changing behavior.
2. Add a regression test that fails on the pre-fix behavior.
3. Implement the smallest safe fix.
4. Run targeted tests.
5. Run the relevant integration/synthetic-browser tests.
6. Commit the package separately with a descriptive message.
7. Do not mark the package complete if the reproduction cannot be explained.

After all blocker packages:

1. Run the full offline suite.
2. Run clean checkout/export verification if the existing workflow supports it.
3. Push the branch.
4. Do not run live HH/Habr/SuperJob/GeekJob/Form/Telegram actions.
5. Produce a final report mapping every audit finding to code + test + commit.
6. Stop. Do not begin Controlled Resume Tailoring.

---

# Freeze blockers

## Package 1 — durable external-action boundary

### A1 — P1: HH/Habr repeat submit after uncertain outcome

Confirmed behavior:
- HH may perform a second DOM submit when confirmation of the first submit is lost.
- HH click fallback may try another click strategy after the first click already happened
  and a post-click wait/inspection fails.
- Habr can click submit again after response timeout.

Required invariant:

> After an external submit command may have been delivered, automatic code must never
> issue another submit for the same attempt. The outcome becomes durable
> `uncertain` / manual-review until explicitly reconciled.

Implementation direction:
- separate "click action happened" from post-click verification;
- post-click timeout/error/cancellation must not fall through to another click strategy;
- remove/guard HH `dom_retry` when the first submit may have happened;
- Habr must not retry a submit merely because confirmation timed out;
- use a small durable attempt record only where needed, following existing
  GeekJob/Google Forms semantics instead of inventing a new subsystem.

Regression coverage must include:
- click delivered + confirmation lost;
- post-click timeout;
- cancellation after possible click;
- next run sees non-retryable uncertainty;
- maximum one submit command per attempt.

### A3 — P1: manual HH approval has no attempt owner

Confirmed behavior:
- two concurrent uses of the same manual token can both reach dispatch;
- approval/feedback can change during browser/model awaits without owning the attempt;
- cancellation / post-click unexpected UI can leave the token retryable.

Required invariant:

> One approval token has one durable owner for an active attempt. Once an external action
> may have happened, the token cannot return to `pending` automatically.

Implementation direction:
- atomic `pending -> applying` claim with owner/nonce;
- owner-bound final state transitions;
- integrate with the HH attempt state from A1 where practical;
- feedback/revocation after claim must be handled explicitly;
- post-action ambiguity -> `uncertain`, not `pending`.

Regression coverage:
- two processes, one token -> exactly one dispatch;
- approval revoked during await -> no dispatch if action has not started;
- cancellation after possible submit -> non-retryable uncertain state;
- stale owner cannot finalize a newer attempt.

---

## Package 2 — bind HH identity to the actual submit action

### A2 — P1: resume can change while Playwright waits inside click

Confirmed behavior:
- the final identity guard can return true;
- `element.click()` may then auto-wait while the selected resume changes;
- submit can therefore carry a different resume than the one just verified.

Required invariant:

> The state that was approved must still be the state being submitted at the actual
> browser action boundary.

The fix must cover at least:
- resume identity;
- cover-letter content if it can change independently;
- questionnaire answers/approval state where the same wait gap exists.

Do not "solve" this with another earlier pre-check.

Regression test:
- submit control disabled/covered;
- final guard passes for target resume;
- DOM changes to wrong resume while Playwright is waiting;
- no request/submit with wrong resume is allowed.

Prefer a browser-behavior regression, not only a mocked click.

---

## Package 3 — immutable approved chat payload

### A4 — P1: approved chat preview is regenerated before send

Confirmed behavior:
- preview can show answer A;
- send path calls generation again;
- answer B can be sent under approval for A.

Required invariant:

> Telegram approval is bound to the exact outgoing payload/revision that was shown.

Implementation direction:
- persist exact approved draft + revision/hash;
- send the stored draft, not a regenerated answer;
- alternative generation creates a new revision and invalidates old send approval;
- incoming message/candidate/profile version changes invalidate approval.

Regression tests:
- preview=A, generator later=B -> send exactly A;
- alternative preview invalidates old button/revision;
- stale candidate/message version blocks send.

### A5 — P1: quick reply drops qualifying conditions

Confirmed behavior:
- an approved message such as "Да, только удалённо и без переезда." can be replaced
  by the platform quick reply "Да".

Required invariant:

> A shortcut may replace an approved answer only if it is semantically and textually
> equivalent for the approved intent. Otherwise send the approved full text.

For the stabilization pass prefer deterministic safety over semantic cleverness:
- if the approved text contains qualifiers beyond a plain yes/no, do not collapse it
  to a quick reply.

Regression tests:
- qualified yes/no remains full text;
- exact plain yes/no may use the shortcut if the existing path is otherwise safe.

---

## Package 4 — exact Google Forms readback

### A6 — P1: actual DOM values can differ from approved values while fill is reported successful

Confirmed behavior:
- extra checkbox options may remain selected;
- a non-empty subset of desired options may be accepted as success;
- failed text readback can substitute the expected value instead of proving actual DOM state.

Required invariant:

> `filled` means the full actual form state equals the approved state for every
> field that will be submitted.

Regression coverage:
- extra checkbox remains selected -> block;
- desired checkbox missing -> block;
- one of two desired options unavailable -> block;
- text readback failure -> block;
- only exact readback allows submit workflow to continue.

Do not weaken workflow ownership/uncertain semantics that are already correct.

---

## Package 5 — facts provenance

### A7 — P1: extract-facts output becomes trusted evidence without proving source support

Confirmed behavior:
- model extraction output is saved into `facts.json`;
- downstream grounding can then use it as confirmed evidence;
- an unsupported extraction can therefore certify its own later claim.

Required invariant:

> Model-generated extraction is not automatically equivalent to user-confirmed or
> source-proven evidence.

Minimum acceptable stabilization fix:
- preserve provenance for extracted facts;
- unsupported sensitive facts such as consent, relocation, salary, identity,
  employment duration or similar decisions must not become confirmed evidence merely
  because the extractor emitted them;
- either verify extracted claims against the source resume deterministically/with a
  separate constrained evidence step, or store them as unconfirmed until user approval.

Regression test:
- resume contains no relocation consent;
- extractor returns `willing_relocate=true`;
- downstream answer grounding must not accept "готов к переезду" as confirmed evidence.

Do not expand this into a general knowledge graph.

---

## Package 6 — destination identity after navigation

### A8 — P1: Habr/SuperJob can submit on the previous page after navigation failure

Confirmed behavior:
- page currently shows vacancy A;
- vacancy B is approved;
- `goto(B)` fails before changing the page;
- code continues and can click controls on A.

Required invariant:

> Navigation failure or destination-identity mismatch means zero external apply/submit
> actions.

Apply to both:
- Habr Career native/browser path;
- SuperJob native/browser path.

Regression coverage:
- previous page A + failed goto(B) -> zero click/submit;
- wrong resulting URL/identity -> zero click/submit;
- only a positively verified destination can proceed.

---

# Non-blocking follow-ups to fix in the same pass if local and low-risk

These are not reasons to redesign the project and are not blockers by themselves.

## A9 — P2: semantic corruption in HH guard state

Valid JSON with invalid date/cooldown fields must not silently disable apply limits.
Add schema/semantic validation and fail closed while preserving the damaged state.

## F4 — P2: `seen.py` corruption recovery is fail-open

A corrupted seen history currently recovers to an empty mapping. Prevent a corruption
event from making old vacancies appear safely new for auto-apply.

## F5 — P2: cover generation does not enforce `finish_reason == "stop"`

Incomplete/truncated cover generation must fall back safely rather than being treated
as a complete draft.

## F6 — P2/P3: overly broad operational logging

Avoid logging full `apply_result`, questionnaire answers or unnecessary cover text.
Keep operational metadata while minimizing candidate PII in logs.

If any follow-up becomes invasive, leave it documented for the next maintenance pass
rather than delaying blocker closure.

---

# Explicit non-findings / do not "fix"

Do not spend this pass on the following unless new current-main evidence proves a bug:

- old blacklist race claim that ignored the current re-check in `_dispatch_apply`;
- Google Forms double-submit claim that ignores current durable
  `claim -> submitting -> submit_uncertain` ownership;
- Matcher NaN/Infinity claim already rejected by current schema validation;
- emergency Ollama text fallback handling of image/vision requests, which is currently
  guarded;
- architecture purity or replacing JSON state with a database;
- AI Gateway work;
- Controlled Resume Tailoring.

---

# Freeze gate

The stable baseline may be frozen only when:

- A1–A8 are fixed or a finding is disproven with a reproducible current-main test;
- all new regression tests pass;
- full offline CI is green;
- no blocker was "fixed" by disabling an unrelated safety guard;
- no real external action was required for validation;
- a short independent re-review confirms the repaired invariants.

Only after that:
- finalize `v0.8.0`;
- update roadmap/checkpoints;
- start Controlled Resume Tailoring.
