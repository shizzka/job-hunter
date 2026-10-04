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
