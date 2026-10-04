# Independent re-review remediation — 2026-10-04

Branch: `audit-remediation-2026-10-04`. Starting HEAD: `078fbd3`.
Scope: R1/A2, R2/A4, R3/A6, R4/A8, and local N1/N2 only. A1/A3/A5/A7 are
not reopened. No architecture refactor, Tailoring, main change/merge, version
bump, production mutation or live application/Form/chat/Telegram action.
All browser cases use real Chromium with synthetic DOM and intercepted requests.
Evidence logs: `/tmp/jh-rereview-evidence/`.

## R1 / A2 — associated successful controls and submitting payload

Before fix: `tests/test_rereview_r1.py` produced **14 failed / 2 passed**.
Disabled/covered Playwright click waits allowed outside controls associated with
`form="approved"` to add/change resume, letter and consent values or lose their
form owner. Synthetic submit recorded the resulting actual FormData entries.

Fix stays in the existing HH boundary. Snapshot the complete associated controls
from `form.elements` together with descendant controls, preserve element identity
and association, and compare serialized FormData at click/submit. Resume aliases
and the complete enabled letter controls must match the approved values.
Bind actual submitter state and payload, plus form destination/method metadata.
DOM requestSubmit also binds its actual submitter before invocation. Dialog-only
surfaces keep their DOM control guards; no fictitious form association via null.

Targeted tests: **160 passed**, including R1, A2 auto-wait races, root/control
review, resume identity, HH client/forms and unexpected-UI guards.

Full isolated offline suite after R1: **2233 passed**, 171.79 seconds.

## R2 / A4 — exact approved composer at actual send event

Before fix: `tests/test_rereview_r2.py` produced **8 failed / 2 passed**.
During disabled/covered click auto-wait, changed textarea text, replaced editor,
replaced composer root (preserving the old button), and an added payload control
all reached the synthetic send handler. The recorded text/root came from the
current page, not the approved draft.

Fix stays in `hh/chat.py`: bind exact answer/editor, common composer/form root,
actual send control, control identities, values/FormData and browser URL before
the final durable callback. A synchronous click/submit capture guard validates
that binding at dispatch; changed state suppresses page handlers. Persistent
approved draft/version semantics and plain yes/no shortcut rules are unchanged.
Browser readback rejects a blocked event before success verification.

Targeted run: 163 passed plus two legacy ordering doubles requiring explicit
arm/readback support. After accurate mock updates, **26 final targeted tests
passed**, including all 10 Chromium cases, low-level chat positives and ordering
checks. No production guard bypass for doubles was added.

Full isolated offline suite after R2: **2243 passed**, 186.99 seconds.

## R3 / A6 — approved Forms values at actual submit boundary

Before fix: `tests/test_rereview_r3.py` produced **12 failed / 2 passed**.
Both native form submit and custom role=button handlers dispatched after a
successful exact readback while a covered click waited. Later text, extra/missing
checkbox, hidden control, item or root mutations were consumed by the handler.

Fix stays in `google_forms/filling.py`. Successful filling seals an approval
against the supplied approved rows, actual question/control references, full
DOM/FormData state and URL. The submit control binds to that sealed approval
before the durable callback; synchronous click/submit capture revalidates exact
approved values at dispatch. No fresh snapshot can silently approve a later
mutation. Prior-page question bindings and unchanged controls are retained;
mutated hidden prior controls, added controls, or unavailable prior bindings
block readiness. Lost prior bindings require manual review rather than an
unverified automatic submit. Existing workflow/version/owner checks remain.

Targeted run: 190 passed plus two ordering doubles needing bind/readback support.
After accurate mock updates and added prior-page browser regressions, **39 final
targeted tests passed**. The 20 Chromium cases cover current/prior approved values,
native/custom dispatch, lost bindings and unchanged positive controls. The final
claim still follows all scroll/bind awaits immediately before click.

Full isolated offline suite after R3: **2263 passed**, 217.03 seconds.

## R4 / A8 — native destination/context at actual external click

Before fix: `tests/test_rereview_r4.py` produced **32 failed / 8 passed**.
Real ElementHandle and Locator click auto-waits admitted changed URL, vacancy ID,
hidden vacancy control and replaced root for Habr/SuperJob apply and submit.
Injection wrappers only introduce the DOM race; dispatch uses real Chromium click.
All requests are fulfilled with synthetic UTF-8 HTML/response objects.
An additional **2 failed / 2 passed** demonstrated changed outside form-associated
vacancy_id in actual FormData after the first event guard implementation.

Fix is local to the existing Habr/SuperJob clients. Bind the actual control/root,
verified source URL and vacancy ID, observable context metadata, full associated
controls and FormData/submitter before clicking. A synchronous capture guard
rechecks that exact binding at click/submit. Known replacement Locator controls
also hit the guard; they cannot bypass it by differing from the old handle.
Blocked readback stops the caller before any subsequent action. Habr's existing
durable no-replay policy is retained; no new state service or framework is added.

Targeted tests: **230 passed** after the primary fix. After the associated-payload
extension, **67 final targeted tests passed**, including all 44 new Chromium
cases and the existing destination/no-replay positive and negative controls.

Full isolated offline suite after R4: **2307 passed**, 287.89 seconds.

## R1 supplemental — successful submitter values

A further actual-FormData review reproduced **3 failed / 1 passed**:
reserved `resume_id`/`letter` names on the submitter introduced unapproved
values; inherited-disabled resume controls produced a payload without a resume.
`valid(submitter)` now requires successful resume identity for native forms
and exact successful letter values, including the actual submitter. DOM-only
dialog validation remains fail closed. No dispatch occurs for those three cases.
Targeted suite: **69 passed**.

Full isolated offline suite after supplemental R1: **2311 passed**, 310.12 seconds.

## N1 — positively proven zero dispatch remains retryable

Before fix: **5 failed** real Chromium behavioral tests showed zero external
handlers yet native/chat/Form attempts became sticky uncertain. An additional
Habr reserved-but-rejected-before-command reproducer failed independently.

Local fix: capture guards track whether any click/submit was admitted and bind
readback receipts to the action nonce/control. Only an owned, positively blocked
receipt with no admitted event (or the native DOM command's explicit pre-dispatch
False) restores preparation/failed status. Habr's pre-command rejection also
records zero action. Current manual ownership consumes the native receipt without
allowing a stale owner to clear another claim. The claim remains held until
finish; native `acting` still forbids a second command in the same attempt.
Missing/replaced receipts, click timeouts/cancellation and admitted events remain
uncertain/non-retryable. No automatic uncertain replay or generic false=>failed
conversion was added.

Targeted final suite: **125 passed**, including 12 new N1 cases, browser zero
dispatch, lost/stale receipt, admitted click then blocked submit, state ownership,
and previous no-replay/cancellation/durability checks. Nine old Forms doubles
needed accurate optional callback/nonce signatures; no guard bypass was added.

First full N1 run: 2322 passed / one Habr pre-command URL-rejection return-path
failure (zero action was preserved). Restored the ordinary negative result
instead of propagating the exception after a positive zero receipt. Corrected
targeted suite including the prior A8 navigation regressions: **132 passed**.

Full isolated offline suite after N1: **2323 passed**, 303.71 seconds.

## N2 — oversized preview rejection before notifier transport

Before fix: **4 failed / 1 passed**. Raw oversized and HTML-expanded oversized
answers, with/without a screenshot, made zero notifier calls yet were reported
as notification_uncertain and blocked a new short alternative. A transport
exception after a fake notifier call correctly remained uncertain.

Fix stays in the existing chat responder. Pure caption/markup rendering checks
the full approved preview before mark_acting. Local rejection clears that draft
and finishes the current owner as failed without preview/reply counters or
notifier delivery; a new short alternative gets a fresh revision and is shown
in full. The notifier still performs the same check; no truncation or weakened
transport guard was introduced. Exceptions after invoking transport keep the
existing uncertain/no-replay behavior.

Targeted suite: **80 passed**, including all five N2 cases, stored-draft approval,
quick replies, notification workflow transactions and low-level chat tests.

Full isolated offline suite after N2: **2328 passed**, 301.62 seconds.
