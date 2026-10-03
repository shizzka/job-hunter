# Candidate-answer grounding and advisory analysis safety

Native HH questionnaires, HH chat drafts and Google Form generation capture a
candidate snapshot before their first model/browser await. Multi-page previews
keep that context throughout navigation and contact/fact re-preparation. Models,
resume, positioning, contacts, facts and salary preference remain operation-local;
context is restored on success, failure or cancellation.
The provider client is captured with the candidate, before navigation; missing
original credentials cannot cause a switch to a later profile's client. Bound
operations use captured KB content instead of rereading another profile's KB.

A separate bounded evidence check evaluates each generated answer or template.
Draft confidence, HR requirements/options and general knowledge are not evidence
of personal experience or consent. Missing experience is unknown, not a negative
claim. Only confirmed facts (including legacy flat facts and confirmed interview
entries) can provide fact citations; inferred/weak/forbidden wording is excluded
from the evidence block. Local checks require complete answer/sentence coverage,
literal current-source quotes and numeric support.
Forbidden-claim constraints travel separately, with priority over an old resume
statement; they can never serve as positive evidence citations.
Truncated/malformed checks or provider failures leave answers skipped/manual,
not invented best guesses.

Known contact fields and explicit confirmed device/date fields use the captured
values directly. Prior cached model answers remain visible for human review but
are not automatically filled without current proof. Explicit reviewed drafts and
manual edits retain their existing approval/revision workflow.
Generated Google choices use exact actual option labels and one canonical plan
for checking and filling: text fields cannot launder claims through `options`,
and substring matches cannot expand a verified label into a stronger claim.
The broad account-email checkbox auto-click is disabled. Required technical
email collection stays manual when it cannot be proved/reviewed; ordinary
consent questions follow the same grounded/reviewed answer flow as other fields.

Native HH choice indexes are actual DOM indexes, including gaps for hidden or
disabled options. Before filling/submitting, the question structure and original
resume/letter approval are checked again. Exact native field values/selections
are read back before submission and again in the last-click guard. A changed,
missing or ambiguous field blocks submission. Compatibility helpers and injected
fake sessions are not independently certified browser/account workflows.

The verifier is model-assisted semantic entailment, **not a guarantee that every
possible hallucination is detected**. Operational intent, ambiguity and unknown
facts still require human review. No real form/chat send is performed by ordinary
tests; browser regressions use synthetic HTML with all external requests aborted.

Resume analysis remains advisory, not Resume Tailoring: no HH resume is edited.
Only complete nonempty responses can be published. Exceptions expose their class
only. Publication compares captured source/output bytes under cooperative locks;
stale analyses cannot overwrite a changed resume or advice. Private atomic
publication uses the existing fsync-backed writer. This is a per-file contract,
not a cross-file transaction or universal resume provenance certification.
Both the wizard and CLI analyze the exact bytes captured for publication, not an
earlier local draft or a second read from a potentially changed source file.

Regression groups: `test_answer_grounding_regressions.py`,
`test_answer_dom_safety.py`, `test_resume_analysis_regressions.py`, plus native
HH/form/chat compatibility suites. Live DOM/account/send acceptance remains
separate from offline evidence and needs the applicable user authorization.

## Search cancellation

The native HH page-search operation has its own awaited task. On cancellation,
Playwright protocol callbacks belonging to that task are cancelled before the
caller's cleanup closes the browser. This fixes the reproduced unobserved
`TargetClosedError` during search interruption without hiding real failures,
shielding work from cancellation or adding retries. The offline Chromium
regression aborts every external request and observes the event-loop errors.
This is scoped to search, not certification of every browser cancellation path.
