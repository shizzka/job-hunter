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
