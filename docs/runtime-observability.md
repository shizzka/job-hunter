# Runtime diagnostics

Search diagnostics use the existing profile-owned `analytics_events.jsonl` and
`run_history.jsonl`. `analytics.get_run_summary(run_id, ...)` folds those records
with `run_summary.build_run_summary`; no LLM or text-log parsing is involved.
Existing funnel counters and decision semantics remain unchanged. `evaluated`
counts completed scored calls through the matcher observation boundary; deferred
results and reused retry evaluations are not counted as scored calls.

## Interfaces

- Telegram: **📊 Последний запуск**, `/log`, or `/log <run_id>`.
- Telegram: **🧾 Raw log** or `/raw_log` retains the existing private logfile tail.
- CLI: `./run.sh --profile qa run-summary [run_id]` returns JSON.
- Python: `analytics.get_run_summary(run_id, events_file=..., history_file=..., profile=...)`.

`/hunter_log` and the old search-log button label also select the summary.
Chat log commands keep their existing behavior. All lookups use the selected
profile's storage, without activating another profile in the bot process.

## Additive event contract

The existing schema version 2 envelope supplies `event_id`, `run_id`,
`created_at`, `recorded_at_utc`, `profile_id`, `source` and `vacancy_id` where
applicable. Failure events add:

```json
{
  "event": "stage_failed",
  "run_id": "search-example",
  "stage": "matcher",
  "source": "hh",
  "vacancy_id": "123",
  "failure_id": "incident-example",
  "reason_code": "LLM_PROVIDER_EXHAUSTED",
  "error_kind": "TimeoutError",
  "provider": "primary,backup",
  "model": "configured-model",
  "outcome": "error",
  "severity": "error",
  "retryable": true,
  "fallback_status": "failed",
  "continued": true
}
```

Repeated exhaustion of the same `(stage, source, reason_code, provider, model,
retryable, fallback_status)` within one observed run reuses the incident ID and
emits `failure_observed`. This groups repeated failures of the same provider
chain; it does not claim that they were one HTTP request. Distinct signatures
remain separate incidents. Recovered provider attempts remain usage events and
do not become primary run failures.

Deferred decisions refer to the failure recorded for that vacancy and matcher
stage. A propagated provider exception carries its originating ID; its terminal
failure has `parent_failure_id`. Final pending decisions refer to the terminal
cause. An unrelated exception is a separate failure even when it follows a
provider error at the same stage. Unknown causes use `UNKNOWN_ROOT_CAUSE`.
Existing reason-breakdown keys are retained for compatibility.

Retryability records the existing provider retry/fallback classification. It is
diagnostic metadata and never authorizes another external submit or changes
retry policy. Fallback status describes the observed terminal chain:
`failed`, `unavailable`, or `not_attempted`.

## Summary rules

The earliest timestamped root incident is primary (stable observation order is
used when timestamps are absent). Propagation events resolve through
parent IDs; decisions are deduplicated by `(source, vacancy_id)`, preserving an
uncertain application outcome. Affected count is the number of distinct vacancy
identities linked to the incident, not the number of log lines. Uncorrelated
legacy failures have unknown affected counts and IDs.

Status is `success` for a completed run without observed failures or unresolved
outcomes, `partial` when problems coexist with completed scoring/applications
(or a source collected data while another failed), and `failed` when a failed
run has no such progress. Deliberate run limits produce `partial` plus
`stop_reason=RUN_LIMIT`, without inventing an error. A missing final checkpoint
is `incomplete`; insufficient legacy data is `unknown`.

Interrupted counters are lower bounds or unknown. Missing fields are `null`,
not invented zeros. Existing application receipt reconciliation is reused; the
summary never concludes that an uncertain application was not submitted.

```json
{"run_id":"search-ok","profile":"qa","status":"success",
 "counters":{"fetched":23,"evaluated":23,"matcher_pass":5,"applied":2},
 "primary_failure":null,"failure_count":0}
```

```json
{"run_id":"search-failed","profile":"qa","status":"failed",
 "counters":{"fetched":23,"evaluated":0,"applied":0,"deferred_unscored":23},
 "failure_count":1,
 "primary_failure":{"failure_id":"incident-example","stage":"matcher",
   "source":"hh","provider":"primary,backup","reason_code":"LLM_PROVIDER_EXHAUSTED",
   "affected_count":23,"downstream_events":{"deferred_unscored":23},
   "retryable":true,"fallback_status":"failed"}}
```

Examples show only the relevant subset of the returned fields.

## Deep diagnostics

Text logs remain local debug artifacts. Repetitive deferred/not-processed
decision lines use DEBUG; run finalization emits one pending-count line at INFO.
The JSONL journal retains every individual decision for precise investigation.

`debug_artifact` events contain `artifact_type=apply_trace`, a `trace_id` and the
existing trace JSONL path. The trace directory contains its summary and existing
screenshot/HTML references; heavy payloads stay there. RunSummary exposes these
references, including related failure IDs, and Telegram displays at most two.
Use the JSON summary for untruncated paths. Artifact references remain subject
to the existing trace retention policy.

Legacy records are read without migration. Summaries currently scan the same
local journals as existing analytics; no index, service or database is added.
If diagnostics writes fail, search/apply behavior still follows its original
result and exception. Missing correlation cannot be reconstructed from logfile
text and is displayed as unknown.
