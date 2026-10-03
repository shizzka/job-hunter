# Persistent writer inventory — 2026-10-03

Scope: Python production modules and smoke scripts in the local Job Hunter
checkout. Static searches covered `open`/`os.open`, `write_text`/`write_bytes`,
`json.dump`, atomic helpers, repository `save`/`update`, logging handlers and
browser screenshots. Tests, venv, generated output and network JSON payloads are
not persistent-state writers. Shell launch/log handling still needs a separate
pass. This is an inventory, not certification of every workflow or deployment.

## Covered groups

| Writers | Current protection / evidence |
| --- | --- |
| `seen`, `hh_guard`, analytics JSON state, `hh_resume_pipeline`, `facts` | Shared locked atomic updates, corruption backups and propagation of read I/O failures. Existing state-store and module regression tests; candidate-facts backup uses private atomic JSON. |
| `runtime_control`, Telegram access/clients/resume limits | Private atomic JSON/PID, transactional registry mutations and lifecycle/owner locks. Registry/lifecycle follow-ups in the audit document. |
| `telegram_bot` authoritative state and agent runtime status | Transactional bot-state mutations, shared runtime sidecar lock, separate process lifecycle locks. `test_bot_state_transactions`. |
| `manual_apply_queue`, `candidate_interview`, `company_blacklist` | Transactional updates, corruption retained in place, private/fsync-backed writes; legacy queue/blacklist lock names retained. `test_queue_fact_state`. |
| `agent` resume download / analysis, `setup_profile` env / resume / analysis | Private atomic text writes; download captures the destination and resume-selection variants before the first await. Setup env publication cannot clobber a concurrent creator. `test_text_env_state`. |
| `profile.create_profile`, `migrate_profile_note` note creation | Complete private temporary file published by same-directory hard link, then temp unlink and parent fsync. Existing files, including symlinks, cannot be replaced. Unsupported link/filesystem operations fail rather than falling back to truncation. `test_text_env_state`, existing migration tests. |
| `profile.update_profile_env`, HH-auth resume-slot update, note salary migration | One stable `.profile.env.lock` across read/mutate/atomic replacement. Comments/unrelated keys retained, values normalized; migration inserts only missing keys, including preservation of existing empty values. `test_text_env_state`, profile/auth/migration tests. |
| `google_forms/drafts.save_answer` / `.supersede`, `state_store/google_forms.remember` | Protected JSON reads, transactional per-file updates and a short `.google_form_workflow.lock` coordinating preview eligibility with edits. Terminal `remember` records cannot be replaced; first successor wins, repeated identical successor is a no-op. `test_form_state_transactions`. Async submit/recheck remain open below. |
| `notifier.notify_stale_cookies` | Protected transactional attempt claim before network await, five-minute failure retry cooldown, daily success cooldown and owner-checked completion. Disabled sources ignored. `test_cookie_warning_safety`; the smoke-isolation incident and correction are in the audit document. |
| `state_store.matcher_deferred`, agent Matcher retry queue | Per-profile captured path, protected transactional JSON, private/fsync-backed writes, cooldown without expiry, revision-checked removal only after durable handling. Unknown sources/schema, corrupt reads and write failures block instead of resetting state. Disabled sources are retained; thread/process contention and downstream cancellation/error/HH guard retention are covered by `test_matcher_deferred_queue` / `test_matcher_deferred_safety`. No file lock crosses an await; this is not an external submission claim. |
| HH-auth text imports/catalog, auth/CAPTCHA IPC; Habr/GeekJob/SuperJob cookies/auth | Already use private atomic text/JSON. Atomic serialization is covered, but async destination ownership and workflow concurrency are not globally certified; see follow-ups. |

## Open follow-ups

| Priority / paths | Remaining work |
| --- | --- |
| Async form/chat workflows: `google_form_filler`, `commands/google_forms`, `telegram_app/forms`, `state_store/chat_responder`, `hh_chat_responder` | Synchronous form mutations are covered above, but async submit still saves a pre-await whole preview snapshot; eligibility checks before browser awaits are not a submission claim. Recheck approval/edit-version races, Telegram pending-form snapshot writes and chat persistence need separate regression groups. Do not hold file locks across network/browser awaits; do not declare exactly-once submit from atomic storage alone. |
| `hh_response_counter.save_snapshot` | Previous snapshot / delta is calculated between separate `load` and `save` operations. Audit ordering of concurrent refreshes before declaring state/resume complete. |
| `hh/browser._save_cookies`, browser session callbacks and other cookie clients | HH has file fsync + atomic replace, but no parent-directory fsync. Capture cookie destination/session ownership before awaits; distinguish intentional whole snapshots from read-modify-write and avoid another profile's destination. |
| `agent._append_run_history`; `telegram_bot._append_chat_ai_audit_event` / `_append_debug_log` | Buffered append without explicit private mode, shared writer lock or durability contract. Test concurrent writers, partial-tail recovery and serialization failures; preserve append semantics instead of replacing the entire journal. |
| `analytics._append_event` | Already private, inode-locked append with truncated-tail separation and optional file fsync. Do not classify as unprotected append. Review reader handling, create/rotation durability and profile destination capture as a separate contract. |
| `debug_trace._private_write_text` / `.event` | Private diagnostic artifacts, not authoritative business state. Text uses truncation and a single `os.write`; JSONL also assumes a full write. Check short writes, concurrency and atomic summaries; do not publish artifacts containing personal data. |
| Legacy HTML/screenshots in `agent`, `hh_client`, `hh/apply`, `hh/resume`, `hh/chat`, `hh_chat_responder`, `superjob_client`, `habr_career_client`, `google_forms/filling` | Non-authoritative diagnostics; some HTML is directly truncated and screenshots are browser-owned writes. Audit private permissions, unique names, redaction and destination ownership. CAPTCHA has existing private-temp cleanup coverage; do not broaden that claim to all screenshots. |
| CLI/bot `FileHandler`, subprocess logs in `runtime_control`, launcher shell | Operational append logs, not JSON control state. Review privacy, multi-process writes and retention separately; log history is not a reason to mark general atomicity complete. |
| `profile._acquire_lock` PID metadata | Intentionally writes the held flock inode. Do not replace this inode atomically or unlink it: that would break mutual exclusion. PID text is diagnostic; review short writes/mode independently of lock ownership. |
| `scripts/smoke/model_bench` output | Direct diagnostic JSON export; not runtime state, but may contain model response samples. Separate safe-export follow-up. |

## Text/env verification boundary

The initial 17-check regression group reproduces 15 failures / 2 passes on
commit `974058f`, including cross-profile download destination, lost concurrent
env fields, unsafe creation and incomplete migration writes. The test fixture
for late profile creation was corrected and the baseline rerun in a separate
old-tree export. The expanded suite also exercises four spawned processes with
40 retained env fields, eight concurrent file creators with one winner,
file/directory fsync, private mode, symlink refusal, malformed UTF-8 and read
permission failures. All inputs are synthetic; analysis/API/browser calls are
stubbed.

Two additional regressions reproduced selection of the other profile's resume
after the first await, even with the already captured file destination. Both
explicit-ID and title-based selection now resolve against the original variants;
existing resolver callers still default to the current configuration.

Atomic text guarantees are per file, not a transaction across setup's env,
resume and analysis, or migration's note and salary settings. A failure may
leave a completed earlier file; it must not truncate an old file or clobber a
concurrent profile. Failures after publication (for example directory fsync)
can report uncertain durability with the new complete file already visible.
Do not claim rollback to the old file for such post-publication failures.

Sidecar env locks coordinate updated writers only. Already running old code,
manual editors and external non-cooperating writers do not automatically share
the lock. No production process was restarted, no runtime file was modified,
and no real application, browser login or provider request was made in this
group. General roadmap atomicity and `State/resume` remain open.

## Synchronous form verification boundary

Preview and manual-edit JSON/UTF-8/collection corruption now stays in place and
blocks repeated reads/mutations instead of becoming an empty replacement. The
old `items`-schema repair test was intentionally changed to require preservation;
existing production schemas were checked read-only before publication.
Validation covers expected collections/entry shapes, not every field's business
meaning. Missing files still support initial setup.

New short synchronous operations acquire the workflow coordinator first, then
the required per-file sidecar. Draft eligibility is checked after waiting for
that coordinator; writes preserve unrelated records/answers and metadata.
The first successor is retained under contention; a different successor is
rejected, and an identical successor is an idempotent no-op. Normal seven-day
preview expiry and answer/option/required-field behavior remain unchanged.
`remember` refuses to replace an already terminal detail, except an identical
no-op; full snapshot `save` is still available and is not a transactional merge.

These locks protect cooperating updated synchronous writers only. A browser
submission or an already running old writer is not covered by a lock held only
around its final save. `google_form_filler.submit_saved_preview` is intentionally
unchanged in this group and still requires a persisted submit claim, fresh edit
and terminal checks, token-scoped result persistence and cancellation/uncertain
outcome tests. A stale recheck must also not submit user approval for newer edits.
No form-state lock may be held while awaiting browser, model or Telegram work.
