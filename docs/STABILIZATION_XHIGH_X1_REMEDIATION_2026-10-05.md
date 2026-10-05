# Targeted XHigh blocker remediation — 2026-10-05

## Scope and verdict source

Source: [latest independent XHigh targeted re-review of exact b591ec9](/home/q/job-hunter-targeted-rereview-b591ec9.md).
Baseline: `b591ec98913d38f15e0c0b074f106a495300fd3b`.
Separate branch: `targeted-remediation-x1-2026-10-05`.

That verdict identifies **one remaining freeze blocker, X1 / P1**. Its four
failing cases share one root cause. X2 is an explicitly non-blocking P2 and is
excluded from fixes under the user's prohibition on incidental P2/P3 work.
No new audit or search for additional defects was performed.

## X1 — reproduced P1: chat FormData validation dispatches a sender and permits replay

**Original XHigh assertion.** `arm_send_boundary.snapshot/valid` constructs
`new FormData(root)` on the original form. The native constructor synchronously
fires the page's `formdata` handler, which can dispatch a POST before the action
command and before durable `before_send`. A subsequent owned zero-command
receipt clears the claim despite that earlier external action. A native handler
can transform approved A into unapproved B; repeating the same revision then
repeats delivery.

**Exact reproducer.** Unchanged copies of the XHigh
[test_review.py](/home/q/job-hunter-x1-remediation-20261005-evidence/test_review.py)
and [test_chat_transform.py](/home/q/job-hunter-x1-remediation-20261005-evidence/test_chat_transform.py)
were run with `-k readback`. Integrity hashes and isolated commands are in the
[new evidence directory](/home/q/job-hunter-x1-remediation-20261005-evidence/README.md).
Real Chromium uses a native FORM composer, a page `formdata` listener registered
before approval, actual native click and intercepted request bodies. Complete
stored workflow runs actual fill/approval/dispatch/durable state; only model,
incoming history and chat routing are synthetic. All browser requests are
locally fulfilled, including every POST.

**Result on exact b591ec9: REPRODUCED.** Fresh run: **4 failed, 26 deselected,
12.50s**, with invariant assertion failures and no setup errors.
[Before log](/home/q/job-hunter-x1-remediation-20261005-evidence/before.log),
[before observations](/home/q/job-hunter-x1-remediation-20261005-evidence/before-observations.jsonl).

| Case | Baseline handler / click / POST | After fix handler / click / POST |
| --- | --- | --- |
| Arm without command | 1 / 0 / 1 approved A | 0 / 0 / 0 |
| Unchanged stored approval | 3 / 1 / 3 extra readback POSTs, completed | 0 / 1 / 0 extra readback POSTs, completed |
| Late editor mutation and repeat | 1 / 0 / 1 first POST; 2 after repeat, failed/retryable | 0 / 0 / 0, including repeat |
| Native formdata transform to B and repeat | 1 / 0 / 1 first **B** POST; 2 after repeat, failed/retryable | 0 / 0 / 0, including repeat |

The unchanged original composer records its legitimate native click without
posting from that click. Consequently its successful after-case has one click
and zero POSTs. A separate permanent regression below checks a legitimate
native-click FormData sender and requires exactly one actual approved POST.

**Root cause.** One remaining original-form constructor in the chat snapshot
bypassed the existing inert successful-controls helper. The payload helper in
the same validator already used the correct API. Zero explicit click proof was
insufficient because snapshot itself dispatched page code.

**Fix.** One production line in [hh/chat.py](../hh/chat.py) now reads snapshot
FormData through `window.__jhActionBoundary.formData(root)`. That existing
helper uses an inert document copy with live successful-control values,
associations, selections and files; original page listeners do not execute.
All directly used source validators now use that helper for FormData readback.
The synchronous final validation/native-action sequence, per-attempt ownership,
original-document binding, nonce, immutable snapshots, actual submitter checks,
single-use command and conservative post-dispatch uncertainty remain in place.
No pointer fallback, UI-selection heuristic, retry after ambiguous dispatch,
or original-form event suppression was introduced.

**Regression.** New [tests/test_chat_formdata_readback.py](../tests/test_chat_formdata_readback.py):
arm purity, full stored unchanged send, late mutation, native formdata transform,
and exactly one legitimate native FormData POST after dispatch with no second
use of the approval. These **5 tests failed before the fix**:
[regression-before.log](/home/q/job-hunter-x1-remediation-20261005-evidence/regression-before.log).
The native sender check proves the fix does not disable actual page FormData
handlers during the authorized action.

**Exact reproducer after fix.** **4 passed, 26 deselected, 10.04s**.
[After log](/home/q/job-hunter-x1-remediation-20261005-evidence/after.log),
[after observations](/home/q/job-hunter-x1-remediation-20261005-evidence/after-observations.jsonl).
The unchanged reproducer hardcodes baseline SHA in its observer; separate
[run metadata](/home/q/job-hunter-x1-remediation-20261005-evidence/reproducer-metadata.json)
identifies baseline and corrected source, rather than misrepresenting that
annotation as after-run HEAD.

**Targeted result.** **205 passed, 376.40s**: all five new regressions and all
11 suites requested by the latest XHigh review, including existing real offline
Chromium, native/source/Form/workflow, notifier, ownership, navigation,
actual-submitter and uncertainty tests.
[Targeted log](/home/q/job-hunter-x1-remediation-20261005-evidence/targeted.log).

**Fix + regression commit SHA:** `4190e3094c907510cbb8e3cd2beba8d7ab810cfa`.

## X2 — prior reproduced P2, excluded from blocker remediation

**Original XHigh assertion / reproducer.** Pending native Habr/SuperJob Locator
preparation overlaps foreign-document navigation. Owned action stays zero;
terminal cleanup leaves a current-document fence, preventing a later unrelated
settings form submit until manual release of the same owner.
`test_independent_pending_locator_cannot_retarget_new_document[foreign-habr]`
and `[foreign-superjob]` in the source XHigh evidence provide the reproducer.

**Result on b591ec9 / status.** XHigh reports both cases reproduced. This run
did not rerun these P2 cases and does not relabel them NOT REPRODUCED.
[Original observations](/home/q/job-hunter-targeted-rereview-b591ec9-evidence/observations.jsonl).

**Root cause.** XHigh infers a race between current-document release and a
pending document-start fence during cross-origin navigation; the exact renderer
schedule was not separately proven. Actual stale blocking and restoration after
manual owner release were observed.

**Fix / regression / targeted result / commit SHA.** No X2 fix or new regression;
no fresh X2 targeted result; implementation remains the baseline `b591ec9`
behavior. The latest verdict explicitly excludes X2 as an independent freeze
blocker. Fixing it here would violate the user's scope restriction.

## NOT REPRODUCED findings

None among the remaining blockers in the latest verdict. Earlier verified-safe
classes are not new findings and were not subjected to a new independent audit.

## Final validation

**Full offline suite:** **2418 passed, 612.95s (10:12)**, from exact clean archive of
`4190e3094c907510cbb8e3cd2beba8d7ab810cfa`.
[Full log](/home/q/job-hunter-x1-remediation-20261005-evidence/full-clean-export.log).
The old 2413-passed baseline claim was not used as proof for this branch.

**Clean export:** **PASS — 2418 passed**. Full offline suite and clean-export run are
one fresh complete execution from `git archive`, with no .git, production env,
cookies or state; fresh isolated HOME and pytest basetemp. The existing network
guard is unchanged. Existing native notifier transport tests redirect only to
synthetic loopback; all Chromium requests abort or fulfill locally.

**Other checks:** `git diff --check` and `bash -n run.sh` passed.

**GitHub CI / Final HEAD:** the report is committed after local results, and the
existing `offline-tests.yml` is then dispatched on its **exact final HEAD**.
To avoid a commit self-reference or changing the tested HEAD after CI, full final
SHA, run URL, verified CI `head_sha`, status and suite result are recorded in
[final-validation.json](/home/q/job-hunter-x1-remediation-20261005-evidence/final-validation.json)
and the final user response. CI logs and metadata are preserved alongside it.
[Branch-specific offline workflow runs](https://github.com/shizzka/job-hunter/actions/workflows/offline-tests.yml?query=branch%3Atargeted-remediation-x1-2026-10-05).
The report commit changes documentation only after the fully tested fix commit.

## Action limits and stop

No live applications, chat sends, Google Form submits, Telegram sends, production
state/cookie mutation, merge, version bump, tag, Tailoring, AI Gateway,
diagnostics UX, unrelated refactor or dependency change. Only synthetic offline
browser actions and GitHub branch/commit/offline-CI publication were performed.
`main` and `audit-remediation-2026-10-04` were not merged or changed.

After exact-final-HEAD CI verification, STOP. This work requests targeted
re-review; it does not declare READY FOR RELEASE or authorize freeze/merge.
