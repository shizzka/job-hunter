# Observability patch for soak/E2E observation

Branch: `feat/observability-0.8.1`, created strictly from released `v0.8.0`.

## Preconditions and artifact preservation

Before any patch, all release/runtime references matched:

```
BASE_RELEASE_SHA=48abf39bdde926dba5bb15e7f4c5fd9955e69dd4
CURRENT_MAIN_SHA=48abf39bdde926dba5bb15e7f4c5fd9955e69dd4
CURRENT_VERSION=0.8.0
RUNNING_RUNTIME_SHA=48abf39bdde926dba5bb15e7f4c5fd9955e69dd4
```

Release/deploy closeout already existed. Actual runtime checkout:
`/home/q/job-hunter` → `/mnt/work/job-hunter`, main; existing background Telegram
bot, profile `qa`, `--keep-pending`. Cron/daemon/service architecture is unchanged.

Unrelated untracked files were inspected and moved, without deletion or Git
addition, to `/home/q/job-hunter-local-artifacts-before-observability-20261005`:

- `.playwright-cli/`: old local browser YAML evidence/debug snapshots.
- `output/`: old local browser screenshots and private text evidence.
- `tests/test_grounding_timeout.py`: useful uncommitted tests for separate
  grounding timeout experiments, requiring unrelated local functional changes.
- `tests/test_lm_studio_compat.py`: useful uncommitted offline provider wire-format
  tests for separate LM Studio compatibility experiments.

These are not current production state/config/cookies or part of this patch.
The private backup manifest verifies unchanged SHA-256/content/size for all 25
files before/after the move. The production tracked tree and checkout were clean
before creating the separate worktree `/home/q/job-hunter-observability-081`.

## Scope and correlation

Reuse `analytics.py`'s existing journal and ContextVar. Additive fields retain
`schema_version=2`; existing decision/reason/error fields are preserved. No
analytics or protected-state destructive migration is performed. A single unique
`run_id` follows start, decisions, stage failures, UI observations, final journal
summary, run history and Telegram run view. The UUID suffix prevents same-second
run collisions. Task-local observation state is released on success, failure or
cancellation; journal and history destinations are frozen at run entry.

Search starts receive durable append-only incomplete history checkpoints before
client initialization. One final checkpoint and `search_finished` follow client
cleanup. Readers fold checkpoints by `run_id`; records without IDs remain
readable. Hard process termination leaves a visible incomplete checkpoint.
Cancellation/cleanup failure preserve already observed successful applications;
there is no replay or conversion to proven zero-action. Search business returns
retain the legacy `found` and dry-run `applied` values; diagnostic history uses
actual successful application counts and an explicit dry-run-match count.

## Counter definitions and increment ownership

The canonical run funnel is `run.funnel`; canonical per-source values are
`run.source_stats[source].funnel`. Legacy source buckets remain available. The
new `keyword_pass`, `matcher_pass`, `apply_attempt` mirrors are explicit; no new
UI calls keyword-pass vacancies “relevant”. `relevant` remains a deprecated
keyword-pass compatibility alias, including existing retained staged retries.

| Counter | Meaning / increment point |
| --- | --- |
| fetched | Raw source collection encounters, including duplicate cards across queries/pages; existing collector receipt point. |
| already_seen | Existing collector seen check; counts raw encounters, not distinct vacancies. |
| new | Unique post-dedupe/deferred-merge candidates at the existing new-stage point, excluding staged HH retries. |
| keyword_pass | Output of the existing keyword stage, counted once in its retained output loop. Existing staged retries bypass filtering but remain eligible output. |
| matcher_pass | Existing should-apply branch reached after red flags/low-score/manual-yellow handling; existing staged retry approvals also pass this stage. |
| apply_attempt | Invocation of the existing apply workflow after cover/wait preparation, once before dispatch. This is not a native-submit or successful-receipt count; preflight can stop it. |
| applied | Existing `applied_auto` terminal decision, once per candidate within a run. |
| manual | Existing manual-handling bucket count, once per existing handoff; includes failed applies requiring manual work. |
| skipped | Terminal keyword/matcher/blacklist/already-applied rejection decision, once per candidate. |
| failed | Terminal failed-apply decision; can also require manual work. |
| guard_stop | Existing guard-deferral branch, or classified no-cover guard; distinct from successful dispatch. |
| deferred_unscored | Existing unscored matcher/provider-deferral outcome. |
| dry_run_match | Existing dry-run match branch; contributes neither applied nor apply_attempt. |
| not_processed | Collected candidate did not reach a terminal branch: run limit, interruption, or explicitly unclassified. |
| retry_existing | Existing staged HH retry candidate entering this run; not newly discovered. |

These stage counters are not an exclusive partition: e.g. a failed apply can
require manual work. `reason_breakdown` records one terminal classification per
candidate. Precise existing journal reasons/error kinds remain untouched.
Collection/details failures record run/source/stage/error class/continuation;
final `ok` is correlated through `search_finished`. Partial collection failure
retains the existing continue-with-other-sources behavior. Zero-apply diagnosis
uses only these structured fields, and states “причина не классифицирована” when
they are insufficient. No LLM or human-log parsing supplies funnel statistics.

## Human logs, privacy and Telegram

Explicit root-handler channel filters route search/apply/runtime to
`job-hunter.log`, chat responder/shared chat-context calls to
`job-hunter-chat.log` beside the search log. Bot process logging retains its
existing process log. No module-per-file subsystem is added; chat logger
propagation goes to the filtered root handlers. Handlers retain private 0600
append, short-write/locking and reopen-on-rotation semantics. Log failures report
an exception class, never the sensitive record or exception body.

The operational formatter adds safe `run/source/vacancy/stage` context, omits
exception payloads/traceback/stack bodies and redacts URLs/credential headers.
Free generated matcher reasons/red flags, response bodies, captcha answers,
resume titles and workflow tokens are removed from existing operational log
statements. Private generation, resume selection, Forms/chat filling, provider
routing and approval/submit/retry behavior are unchanged. Existing private
analytics fields are not expanded with new candidate payloads.

Telegram search/chat log buttons read only the selected profile's respective
file, without fallback to a different profile/global `/tmp` log. Run result and
freshness are separate checks; fresh log timestamps are informational and do
not make a stopped daemon healthy. Last-run summaries show ID, explicit stages,
reasons and source summaries, including failures/incomplete runs.

## HH unexpected UI

Every newly inspected unexpected UI trigger calls `observe` before notification
claim. Cached rethrows from an already blocked guard do not create extra actual
observations. Notification `claim_notification` retains the original
cooldown/ownership/delivery behavior. The separate observations section stores
only fingerprint, first/last timestamps, occurrence count, affected-run count,
four stage counts and a bounded recent ID sample. No DOM/text/URL/screenshot or
candidate payload is stored in that section.

Retention is capped at 64 fingerprints and 64 recent run IDs per fingerprint.
`affected_runs` increments for IDs absent from that recent deduplication window;
reappearance of an evicted old ID may count again. Exact historical distinct-run
analysis can use the existing journal's correlated `unexpected_ui` events. This
bounded retention policy is diagnostic only and cannot change a browser guard.
Corrupt protected warning state is retained and follows existing fail-closed
policy; no reset is introduced.

## Validation and review handoff

Offline fixtures compare immutable `48abf39` search body with patched search
body on six identical cases: ordinary outcomes, dry-run, guard deferral, manual
yellow zone, per-run limit, and all-keyword rejects. Matcher/apply call traces,
legacy business result fields and durable seen actions are identical.

Regression coverage includes channel isolation/concurrency/rotation, private
sentinels, provider/journal failures, successful action followed by cancellation
or cleanup failure, profile-bound history, legacy records, deterministic diagnosis
and real offline Chromium UI triggers with all requests aborted. The real apply
wrapper is tested with a synthetic successful/error dispatch and a failing
journal: exactly one dispatch, same result/original exception.

Evidence/results are recorded in the private task directory
`/home/q/job-hunter-observability-081-evidence`; final validation is performed on
the committed patch and clean export. No live application, chat/Form submission,
Telegram test/broadcast or manual search/apply run is authorized by this patch.
The v0.8.0 production checkout/tag/main stay unchanged. Review/merge and any
v0.8.1 deployment are separate steps; no general safety audit or roadmap phase
is started.

### Offline harness incident

The first, discarded comparison copied module globals before installing the
synthetic matcher. It inadvertently invoked the default LLM client: five HTTP
requests with dummy `no-key` authentication received 401 Unauthorized. That
attempt used an isolated temporary HOME/config/fixture state, not production
credentials. No platform application, chat/Form submission or Telegram delivery
was dispatched. It is not offline validation evidence, and the task cannot be
reported as having made zero external requests.

The corrected comparison binds the fixture's actual patched globals and forbids
IP socket connections; all six cases then pass. Ordinary pytest already has an
autouse IP network guard, and the Chromium fixtures abort/fulfill every request
locally. Safe incident metadata is retained separately in task evidence.
