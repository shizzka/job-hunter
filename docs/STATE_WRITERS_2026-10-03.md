# Persistent writer inventory — 2026-10-03

Scope: Python production modules and smoke scripts in the local Job Hunter
checkout. Static searches covered `open`/`os.open`, `write_text`/`write_bytes`,
`json.dump`, atomic helpers, repository `save`/`update`, logging handlers and
browser screenshots. Tests, venv, generated output and network JSON payloads are
not persistent-state writers. A subsequent pass also covers launch/install
shells and native log handlers below. This is an inventory, not certification
of every workflow or deployment.

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
| `google_forms/drafts.save_answer` / `.supersede`, `state_store/google_forms.remember` | Protected JSON reads, transactional per-file updates and a short `.google_form_workflow.lock` coordinating preview eligibility with edits. Active/terminal/superseded records cannot be replaced; first successor wins, repeated identical successor is a no-op. `test_form_state_transactions`. |
| `google_forms.workflow`, `google_form_filler.submit_saved_preview`, `commands/google_forms.recheck` | Per-profile durable pre-await attempt claim, fresh revision/owner guard before click, token-scoped completion, sticky active/uncertain attempts without automatic expiry/retry. Recheck successor and original superseded link are one atomic preview-file transition after fresh source/edit check. Callback approval binds displayed revision and CLI requires full revision for recheck-submit. Four-process/30-thread claims, disk/corruption/cancellation/version tests; not exactly-once external delivery. |
| `telegram_app.forms` pending prompts/replies | Transactional bot-state mutation; request nonce before delivery, owner-checked binding/cleanup, pending identity check and synchronous manual-answer save before consumption. No whole-snapshot save or await under lock. Lock order for reply acceptance: bot state -> form workflow -> one form JSON sidecar. Late prompt/reply cannot consume newer state; legacy pending shapes stay readable. |
| `state_store.chat_responder`, `hh_chat_responder.process_one` / `.process_all` | Protected per-chat transactions and durable owner claims before model/browser/notification waits. Latest-question checks after model and before click, durable pre-click phase, owner-scoped completion, counter increment once and completed-message dedupe. Cancellation after possible click becomes sticky uncertain; active attempts have no expiry. Linked-form and suspicious-message notifications also reserve ownership. `test_chat_state_transactions`, `test_chat_workflow_transactions`; see `HH_CHAT_STATE.md` for manual review and compatibility whole-save boundaries. |
| `notifier.notify_stale_cookies` | Protected transactional attempt claim before network await, five-minute failure retry cooldown, daily success cooldown and owner-checked completion. Disabled sources ignored. `test_cookie_warning_safety`; the smoke-isolation incident and correction are in the audit document. |
| `state_store.hh_ui`, `hh.ui` unexpected-dialog warnings | Captured per-profile `hh_ui_warnings.json`, protected claim before screenshot/delivery awaits, five-minute attempt cooldown, daily successful-delivery cooldown and owner-checked completion. Corruption/read/write failure preserves state and suppresses delivery. Only fingerprints/timing/attempt metadata persist. Guard-owned images use private temporary directories/files and cleanup on success/failure/cancellation; legacy diagnostic artifacts are not covered. `test_hh_ui_safety`, `test_hh_ui_warning_state`. |
| `state_store.matcher_deferred`, agent Matcher retry queue | Per-profile captured path, protected transactional JSON, private/fsync-backed writes, cooldown without expiry, revision-checked removal only after durable handling. Unknown sources/schema, corrupt reads and write failures block instead of resetting state. Disabled sources are retained; thread/process contention and downstream cancellation/error/HH guard retention are covered by `test_matcher_deferred_queue` / `test_matcher_deferred_safety`. No file lock crosses an await; this is not an external submission claim. |
| Native `hh/browser` cookies and `hh_response_counter` refresh | Captured absolute session paths, browser/context/nonce ownership and cookie-revision CAS; private atomic writes with file/parent fsync. Counter requests reserve durable sequence numbers before HTTP, reject changed cookie sessions and update snapshot/delta against fresh state under one lock. Corruption is retained; no lock crosses network/browser awaits. `test_hh_cookie_ownership`, `test_hh_counter_ordering`, `test_hh_session_regressions`; see `HH_SESSION_COUNTER_STATE.md` for imports, injected callbacks and deployment boundaries. |
| Native Habr/GeekJob/SuperJob browser cookies | Constructor-captured absolute paths, pre-await snapshot, context/nonce ownership and revision CAS over shared cookie repository; protected corruption/I/O, private/file/parent fsync, partial-start and shutdown cleanup. GeekJob HTTP cookie headers retain the constructed profile. HH typed repository compatibility retained. `test_source_cookie_regressions`, `test_source_cookie_ownership`; see `SOURCE_COOKIE_SESSIONS.md`. Not API token/account or submission ownership. |
| Native SuperJob API auth/tokens | Captured destination/configuration, protected reads, revision-bound client and durable pre-network owner claim. Token rotation has no proxy/direct replay; active/uncertain attempts block automatic retries. Unknown metadata retained. `test_auth_state_regressions`, `test_auth_state_ownership`; see `AUTH_STATE_OWNERSHIP.md`. Not account-wide submission or remote validity. |
| Native HH-auth/CAPTCHA IPC and HH resume import | Captured absolute mailbox/import paths, current-request/profile checks and owner-scoped completion. Native imports recheck cookie/context/revision, catalog CAS and original env/resume bytes; unique private exports. Active/partial-publication claims block retries. Same auth test group; cross-file publication is not atomic. Injected callbacks are not native account proof. |
| Run history, Telegram debug/chat audit, analytics and trace JSONL | Serialize before mutation, stable sidecar plus inode lock, short-write loop, private creation/parent fsync and explicit file durability. Partial JSON tails retained/separated; invalid diagnostic lines skipped without erasure. Analytics context captures destination/state before awaits. `test_journal_regressions`, `test_private_journal_artifacts`; append failures after fsync may be uncertain. |
| Native HTML/screenshots, trace summaries and model-bench export | Captured roots, private unique operation directories, private atomic text/bytes and sanitized HTML. Screenshots use private temp capture without global umask over await. Form filenames remain explicit replacements; cancellation cleans only newly owned output. Model bench not executed. Same journal/artifact tests; images/text can still contain personal data. |
| Agent/bot handlers, subprocess logs, profile PID metadata and shell launch/install | Private reopening append handlers; raw inherited stdout remains best-effort, never truncated on handler failure. PID writes complete/fsync on the held flock inode, never replacement/unlink. Shell umask 077 and atomic rendered unit publication; installer not run. See `PRIVATE_JOURNALS_DIAGNOSTICS.md`. |

## Open follow-ups

| Priority / paths | Remaining work |
| --- | --- |
| Injected auth callbacks / explicit compatibility saves | These do not acquire native context ownership automatically. Whole-file save helpers remain explicit replacement APIs; old/non-cooperating writers are outside the lock contract. Native coverage above does not certify arbitrary callbacks. |
| GeekJob apply cache/browser-API account coherence | Separate submission/account/approval workflow contract, not established by cookie persistence or SuperJob API token protection. |
| Diagnostic content and operational log retention | Private permissions are not universal redaction. Existing retained text/images may contain personal data; raw inherited stdout lacks cooperative record framing. No historical production artifacts were deleted or new retention policy imposed. |
| Repository-wide state/resume / factual grounding / live acceptance | Individual writer contracts do not establish full resume provenance, grounded answers, provider fallback, CI or current remote DOM/submit correctness. Final verification remains separate. |

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

The later async-form group adds that persisted claim and token-scoped completion.
Preview/edit coherence remains a short workflow lock; browser work has no held
file lock. Before actual submit, a durable `submitting` marker separates safe
preparation failure from potentially externally delivered outcomes. Active and
uncertain records are retained beyond normal expiry, including after crashes.
No automatic recovery/retry is inferred from elapsed time. See
`GOOGLE_FORM_EDITING.md` for stale-button and manual-review boundaries. Existing
whole-snapshot `save` helpers remain explicit non-cooperating replacements, not
new async workflow mutation APIs. HH chat transactions remain open.

## Subsequent HH chat verification boundary

The following chat group closes those production responder workflow mutations,
not the earlier compatibility whole-save APIs or the remaining inventory rows.
Six synthetic regressions fail on the previous `99c059e` tree: lost concurrent
chat state, four changed-question variants and approval for a nonlatest message.
The new 62-case group additionally covers thread/process contention, retained
metadata/counters, stale owners, cancellation/disk failures and notification
ownership. The full candidate tree passes 1504 tests, without production
credentials or live browser/model/Telegram sends. The existing QA chat-state
schema was inspected read-only; contents were neither exported nor modified by
these checks. This does not certify grounding, resume provenance or live sends.
