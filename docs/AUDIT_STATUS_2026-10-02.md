# JobHunter audit status — 2026-10-02

Scope: the consolidated v0.6.0 report and its second round, checked against the
current working tree. Round prefixes disambiguate repeated BUG numbers. “Fixed”
means a code fix plus offline regression coverage, not a guarantee that external
sites will never change or that every HH/browser path has been exercised live.
The latest batches and control modules are included in this release with the
user's explicit permission.

## Operational findings

| Report IDs | Status / evidence |
| --- | --- |
| R1-002; R2-018 | Fixed: seen, HH guard, analytics and resume-pipeline state use locked atomic updates, preserve corruption and reload per profile. Runtime status, cookie-warning state, candidate facts, resume catalogs and auth/captcha IPC use private atomic writes. Read permission/I/O failures do not reset valid state. Tests: `test_seen`, `test_hh_guard`, `test_analytics_state`, `test_hh_resume_pipeline_state`, `test_audit_remaining`, `test_state_store_json`. |
| R1-003 | Fixed: military markers use contextual regex, not ambiguous substrings. `test_smoke`. |
| R1-004 | Fixed: login hints never mutate process-wide environment. Named profiles use only their own `profile.env`; absent/differently named fields cannot inherit the administrator's global login/phone. Shared hints remain available only to `default`. `test_client_hh_auth`, `test_audit_remaining`. |
| R1-005 | Fixed: rejection has priority over interview/process terms. `test_outcome`. |
| R1-006 | Fixed: launcher parses env as data, never executes it as shell. `test_isolated_smoke`; `bash -n run.sh`. |
| R1-007 | Fixed: Telegram API errors are separate from transport failures; proxy fallback is temporary; `429` respects retry delay. `test_telegram_api`. |
| R1-008; R2-021/022/025 | Fixed: chat scan cap, dry-run preview dedupe, escaped notification fields and unified autosend flag. `test_hh_chat_responder`, `test_state_store_chat_responder`. |
| R1-009; R2-017 | Fixed: async, HTTPS-only allowlisted Google Form redirects, checked before browser navigation. `test_google_forms_urls`. |
| R1-010 | Fixed via explicit profile policy: `VACANCY_FILTER_POLICY=generic` disables QA-only prefilters, clusters, level guards and cover templates. Custom keywords remain profile-local; military exclusions stay active. Default `qa` behavior unchanged. `test_filter_policy`. |
| R1-011 | Fixed: score coercion handles fractional/text values, booleans and non-finite numbers. `test_matcher_cover_letter`. |
| R1-012 | Fixed: private atomic cookie/auth persistence across job sources. `test_hh_browser`, source client tests. |
| R1-013 | Fixed: Telegram HTML truncation counts decoded text and closes tags, preserving whole entities/emoji; private plain document captions stay plain. `test_telegram_formatting`, `test_telegram_api`. |
| R1-014/015/016 | Fixed: datetime parsing, Playwright handle disposal, smoke interpreter portability. `test_analytics`, HH client tests, `test_isolated_smoke`. |
| R1-017/018 | Fixed: shared full-match profile-name validation, argv-only setup command, safe lock-owner diagnostic without deleting lock files. `test_profile_safety`, `test_profile`. |
| R1-019 | Fixed: direct `httpx[socks]` dependency. |
| R1-020 | Fixed: HH guard persists the initial analytics seed even when empty. `test_hh_guard`. |
| R1-021 | Fixed conservative stopping: a seen/short page no longer means end of results. SuperJob honors `more`, Habr/GeekJob honor page counts, HH scans to empty results or configured bound. DOM/server pagination evolution still needs live verification. `test_search_pipeline`, `test_audit_remaining`. |
| R1-022 | Checked: unknown users go to guest handling rather than acquiring an admin principal. Explicit admin-only notifications still use admin keyboards. |
| R2-019/023 | Fixed: success/existing-response detection uses response UI/URL, not vacancy body text; anti-bot detection avoids generic description words. `test_hh_apply`, `test_hh_guard`. |
| R2-020 | Fixed: model-specific fallback TTL returns to the primary provider. `test_proxy_utils`. |
| R2-024 | Fixed: profile-keyed private HH-auth pending/response files. `test_hh_auth_bridge`, `test_telegram_auth_bridge`. |
| R2-026 | Fixed: shared JSON parser rejects non-object values. `test_llm_utils`. |
| R2-027 | Fixed: vision uses the configured client factory even without a primary env key; human escalation remains reachable; callbacks fit Telegram limits; screenshots use unique private paths and are cleaned on success/failure/cancellation; invalid explicit captcha profiles never fall back to another profile. `test_captcha_solver`, `test_audit_remaining`. |
| R2-028 | Fixed: explicit `LLM_PROXY`, SDK shutdown at CLI/bot exit, no secondary matcher singleton, hidden SDK retries disabled. `test_llm_lifecycle`, `test_proxy_utils`. |

## Publication issue: R1-001

The running checkout contains `runtime_control.py`, `telegram_access.py`,
`telegram_resume_limits.py`, `job_hunter_ctl.py`, and imports work locally.
The user explicitly authorized publishing these four modules with the fixes.
They are added explicitly despite the existing local excludes, together with
the existing access/limit tests. No credentials or runtime registries are added;
local excludes and the pre-push hook remain unchanged. The publication uses a
one-time, explicitly authorized hook override. Clean-tree verification is
recorded below; this resolves missing modules, not every possible runtime bug.

The 2026-10-03 registry follow-up below closes access/onboarding/quota mutations
and atomic `runtime_control.write_json_file` / PID persistence. The bot/lifecycle
follow-up below also closes whole-snapshot bot-state mutations and serializes
process check/spawn/register. This is bounded coverage, not a claim that the
entire repository is free of state bugs.

## Groq-only operational switch

`LLM_PROVIDER_ORDER=groq,groq2` is an allowlist, not a preference. No primary,
Ollama, OpenRouter or paid fallback is called under this setting. Unlisted
credentials remain in the private file unchanged. The second slot is used only
when a separate `GROQ2_API_KEY` is configured. Duplicate credentials are skipped
after ordering, so an alias of the former primary can still be selected by name.

Both configured Groq keys returned HTTP 200 for `/models`; no candidate data was
sent during that check. Native GPT-OSS 20B/120B and Qwen 3.8 27B were available.
Legacy Ollama-style model aliases are mapped to those Groq-native IDs.

Sources: [Groq model deprecations](https://console.groq.com/docs/deprecations),
[Groq vision](https://console.groq.com/docs/vision),
[OpenAI Docs on bounded retries](https://developers.openai.com/api/docs/guides/rate-limits).
OpenAI Docs informed disabling SDK retries under adapter-managed fallback;
the installed SDK's async `close()` contract was also inspected locally.

## Verification / limits

Latest completed local full suite: **1056 passed** (21.86 seconds); diff and Bash syntax
checks passed. After validation, Groq-only order was enabled in the private
providers file without changing any existing credentials/settings. One synthetic
JSON probe succeeded on `groq` / `openai/gpt-oss-120b` in 1.09 seconds; it contained
no candidate data. The QA control bot was restarted with `--keep-pending`, reached
`idle`, and is running under the new code. No active searches were interrupted.

Clean publication-tree verification: **984 passed** (22.35 seconds). The Git
index was exported into a fresh `/tmp` directory, without local ignored modules,
and tested using the existing repository venv with an isolated `HOME` and no
`JOB_HUNTER_HOME` override. The lower count is expected: some pre-existing
local-only test files remain excluded. This verifies the published code/tests,
not a fresh dependency install or a live HH application.

Run from the repository venv:

```bash
./venv/bin/python -B -m pytest -p no:cacheprovider -q
git diff --check
bash -n run.sh
```

No real HH application or captcha submission was made for this batch.
External DOM changes, CAPTCHA recognition quality, provider quotas and business
rules still require live observation. Trace artifacts may contain personal
information; never attach the whole runtime directory to a public issue.

## Additional live provider checks before publication

All checks used synthetic text, with no resume/contact data, HH submissions,
runtime analytics writes or persistent provider-order changes. Calls bypassed
fallback for availability probes so that another provider could not hide errors.
Listing a model does not imply inference permission or remaining quota.

| Configured account | Actual inference check |
| --- | --- |
| Ollama primary / ollama / ollama3 | GPT-OSS 120B returns HTTP 429 on all three. The real cover-letter generator therefore returns its 162-character fallback template, not an AI letter. |
| Ollama model catalog | All 17 listed models probed on the `ollama` account: seven return 429, ten return 402. The former Qwen 3 VL model returns 410 on all three accounts. |
| Groq / Groq2 | GPT-OSS 120B replies on both. GPT-OSS 20B and Qwen 3.8 27B return 403 on Groq, but reply on Groq2. The actual production adapter successfully falls back to Groq2 for both aliases. Text probes alone do not verify image recognition quality. |
| OpenRouter / OpenRouter2 | Configured `openai/gpt-oss-120b:free` returns 404 on both; unmapped Qwen 3 VL alias returns 400. No alternate paid model was selected. |
| Gemini | Mapped Gemini 3.1 Flash Lite replies; unmapped Qwen 3 VL alias returns 404. |
| DeepSeek | Mapped DeepSeek V4 Flash replies; unmapped Qwen 3 VL alias returns 400. |

All nine configured accounts returned HTTP 200 for `/models`. Across them, every
distinct native model required by the current task-role settings was probed;
role aliases mapping to the same model were deduplicated. This is not a claim
that every model in the 465-entry OpenRouter catalog was tested. The production
bot remains restricted to Groq → Groq2; old provider credentials are untouched.

The real cover-letter generator returned non-fallback text on Groq (573 chars)
and Groq2 (546 chars) for a synthetic candidate. **Factuality follow-up remains:**
both outputs invented project/scenario details, and one claimed an unsupported
impact on bug-report acceptance/review speed. This happened with real candidate
context entirely replaced by synthetic inputs, so it is model hallucination,
not evidence of cross-profile contamination. Prompt constraints alone do not
guarantee that every generated claim is grounded in the candidate's facts.

### Follow-up: OpenRouter alias repair

Text aliases now default to `openrouter/free` rather than vanished GPT-OSS free
variants. A synthetic text probe succeeded on both configured accounts, selecting
Ling 3.0 Flash Sante and LFM 2.5 2.6B respectively. Explicit role-model overrides
remain authoritative; the production Groq-only allowlist is unchanged.

The missing vision alias is mapped to the free Nemotron 3 Nano Omni model. A
synthetic red-square probe succeeded on one account; the other timed out during
that model's check. Qwen free vision also succeeded once but had failures/timeouts;
Gemma free vision returned 429. These are not guarantees of CAPTCHA accuracy or
free-provider availability. The general free router was unsuitable for the vision
default: one request selected a moderation model instead of describing the image.

Reference: [OpenRouter free routing](https://openrouter.ai/docs/guides/routing/routers/free-router).
### Follow-up: cover-letter grounding and profile snapshots (2026-10-03)

The user authorized the additional candidate-data verification request. Factual
drafts now require a separate evidence-backed JSON check using the same configured
provider chain, without vacancy text or style examples. Local validation requires
every sentence index exactly once, strict boolean support, literal quotes from
known sources and evidence for numeric claims. Rejected or unavailable checks
produce a neutral fallback. The verifier has a 40-second timeout; its transport
errors, rejected drafts and evidence are not logged. Semantic entailment remains
model-assisted, so this is not a guarantee that all hallucinations are eliminated.

Generation now uses conditional style accents and lower temperature, and avoids
inferring usual QA duties from tool names. Evidence quotes must be continuous;
separate fragments require separate evidence items. HH retry fallback letters
also omit assumed QA tools and experience. Trace metadata records grounding and
fallback status before letter truncation.

Regression tests reproduced three profile-mixing cases on the previous code:
empty/unknown section selections and a selector exception after an active-profile
switch. Fallbacks now use the original snapshot; all three regressions pass.

Verification after the final prompt refinement: **1102 local tests passed**
(26.01 seconds); the isolated publication-tree export passed **1030 tests**
(26.60 seconds). The earlier targeted group passed 110 tests. The difference
between local/export counts is the pre-existing local-only test set. Bash syntax
and diff checks passed. An offline transport interception of
the synthetic Groq2 diagnostic confirmed two requests, only the hardcoded
synthetic resume in verifier sources, and no network traffic. After explicit
payload/destination consent, a live Groq2 probe rejected invented mismatch-recording
and motivation claims and returned the neutral fallback. The generation prompt
was tightened; a second synthetic probe produced a 148-character factual letter
with continuous source quotes, `grounding_status=verified` and no fallback.
No candidate data or real HH submission participated in these probes. Provider
availability and factual quality remain subject to the limits described above.

### Follow-up: transactional Telegram registries and runtime writes (2026-10-03)

Access, onboarding and resume-analysis usage now load/mutate/save under one
stable sidecar `flock`, shared across threads and processes. Onboarding transitions
read approval/profile/auth state inside the transaction rather than copying a
pre-lock snapshot. The existing owner bootstrap, soft-limit accounting and
explicit application/status transitions are preserved.

Malformed JSON/UTF-8, invalid collection structure and invalid/duplicate user
identities block loading with a restoration error. The original file stays in
place, including on repeated reads: corruption cannot become a fresh empty
registry or reset quotas. Missing files still support first-time setup. Read
permission/I/O failures propagate without replacing valid data. Existing field
normalization remains; this is not exhaustive semantic validation of all fields.

Runtime JSON and PID writes use private (0600), fsync-backed atomic replacement.
PID cleanup holds the same sidecar lock while checking its owner and removing
the file. Serialization/pre-replacement failures keep the old file and clean up
the temporary file. Explicit `save_registry` remains a full replacement API;
normal application mutations use transactions instead of separate load/save.

Before fixes, the new regression group reproduced **21 failures / 2 passes**,
including lost counters and users. The final **43 new tests** cover repeated
corruption reads, I/O/replace failures, concurrent insertions and usage updates,
four spawned processes (40 analyses retained), and profile/approval preservation.
Full local verification: **1145 passed** (27.25 seconds); isolated publication-tree
verification: **1073 passed** (30.27 seconds), using the existing venv. The 72-test
difference is the pre-existing local-only set. Diff and Bash syntax checks passed.
No bot restart, provider request, real HH submission, production registry edit
or OSINT change was made.

The next bot/lifecycle follow-up below completes these two scoped remaining paths.

### Follow-up: bot-state transactions and process ownership (2026-10-03)

Bot user/menu/profile/guest/search settings, health and daily-summary records,
bootstrap and poll offsets now mutate the latest on-disk state under one lock.
No file lock spans a network await. Late poll responses cannot lower a persisted
offset; bootstrap cannot replace an offset established during its request.
Malformed JSON/UTF-8 or invalid control-state structure stays in place and blocks
mutations rather than resetting settings/offsets. Explicit full replacement is
still available for tests/restoration, not normal bot mutations. Frozen runtime
paths remain profile-safe.

Runtime-status normalization re-reads inside its transaction. The agent's runtime
writer now uses the same `JsonStore` lock, preventing stale bot normalization from
overwriting a newer progress record. Existing status/error-reporting semantics
are preserved.

Process describe/check/spawn/PID publication and registration share a distinct
lifecycle sidecar lock. Startup releases it before waiting so the child can
register. Registration retries release it between attempts and use monotonic
deadlines. Stop holds lifecycle while waiting, but shutdown PID cleanup uses
only the separate PID lock; graceful child exit is not blocked. Unreadable PID
files propagate permission/I/O failures rather than authorizing a duplicate
spawn. Failed PID publication triggers cleanup of only the newly created child.
An unsuccessful stop retains the owner record instead of claiming it stopped.

The initial new group reproduced **13 failures** on the previous code. Final
coverage adds **23 tests**, including stale await responses, thread contention,
four spawned registration contenders with one winner, graceful cleanup,
PID-publication failures, shared runtime writer locks and corruption/I/O failures.
Final verification: **1168 local tests passed** (29.14 seconds), and **1096 passed**
in an isolated staged publication-tree export (30.39 seconds), using the existing
venv. The 72-test difference is the pre-existing local-only set. The export check
caught an excluded helper basename; the module is published as
`state_store/bot_state.py` without changing local excludes or hook protection.
Diff and Bash syntax checks passed. The existing production bot-state schema
passed a read-only compatibility check without exposing records or modifying it.

No production bot/daemon restart or signal, provider/Telegram network call, real
HH submission, production state edit or OSINT change was made. Locks are advisory:
older already-running code does not retroactively adopt them. Offsets are still
recorded before handling updates; this is not transactional message processing
or exactly-once notifications. PID reuse and external non-cooperating writers
are not fully solved by sidecar locking. Queue/fact/blacklist paths are covered
by the next follow-up; remaining repository-wide writers still need inspection.

### Follow-up: queued decisions, confirmed facts and employer exclusions (2026-10-03)

Queue operations and confirmed-fact additions now load/mutate/save through
`ProtectedJsonStore` under one stable lock. JSON/UTF-8/collection corruption
blocks repeated reads/mutations and leaves the original file in place;
permission/I/O errors propagate instead of creating empty replacement state.
Validation covers the expected collection and entry structure, not every field's
business meaning. Missing files still support first-time setup.

Queue and blacklist retain their existing `<stem>.lock` sidecar names, so older
cooperating code still locks the same inode. Paths are resolved once before
waiting; queue and blacklist mutations cannot re-resolve another active profile
inside the critical section. Facts use a shared per-file sidecar for all new
writers. Older fact writers do not retroactively acquire this new lock.

All three writers use unique private (0600) temporary files, data fsync, atomic
replace and parent-directory fsync. Data-fsync/serialization/replace failures
before replacement retain the old file and clean temporary files. A directory
fsync failure after replacement does not roll back an already replaced file;
hardware/filesystem failure guarantees are not implied by these tests.

Queue feedback/status/snooze behavior, fact case-insensitive deduplication and
100-fact cap, metadata preservation, employer matching and profile isolation
remain unchanged. Unknown queue tokens and duplicate facts do not create or
rewrite JSON; listing/pruning remains read-only for queue data. Explicit full
replacement helpers remain for restoration/tests, not application mutations.
Locks/parent directories may be created by reads, but state JSON is not saved.

Initial new regression baseline: **18 failures / 5 passes**. The final **42 new
tests** exercise corruption/UTF-8/schema, permission/replace/data-fsync errors,
concurrent updates and duplicate facts, profile changes at lock acquisition,
legacy sidecar names and unchanged no-op/listing behavior. Four spawned workers
retain all 40 insertions independently for queue, facts and blacklist.
Local full verification: **1210 passed** (38.17 seconds); isolated staged
publication-tree verification: **1138 passed** (41.03 seconds), using the existing
venv. The 72-test difference remains the pre-existing local-only set.
Targeted verification: **64 passed**. Read-only checks across existing profiles found compatible schemas
for three queues, two fact files and two blacklists, without exposing records
or modifying state. Diff and Bash syntax checks passed.

No production process restart/signal, provider/Telegram request, real HH
submission, production state edit or OSINT change was made. This does not provide
exactly-once vacancy submission or complete repository-wide state safety.

### Follow-up: resume/setup text and profile env transactions (2026-10-03)

Resume downloads, CLI/setup analysis and setup resume persistence now use
private atomic text with file and parent-directory fsync. Download captures both
the output path and HH resume-selection variants before its first await; ID and
title resolution cannot switch to another profile's settings while downloading.
Existing resolver callers retain their current-config behavior.

Profile/setup env creation and legacy note migration publish a fully written
private file by same-directory hard link, refusing an existing target even if
another creator appeared after the initial existence check. No destructive
fallback is used if that operation is unsupported. Profile edits, HH-auth resume
slots and optional salary migration share one stable env sidecar across the
entire read/modify/atomic-replace operation. Other keys/comments and last-duplicate
key behavior remain; salary migration preserves existing values, including empty
ones, and adds its comment only when inserting missing settings.

The corrected initial 17-check baseline on old commit `974058f` gives **15
failures / 2 passes**. Two more regressions reproduced wrong-profile resume
selection on the intermediate fix. Final coverage adds **32 tests**, including
four spawned env writers retaining 40 keys, eight simultaneous atomic creators
with one winner, private permissions, fsync/link/replace and read/UTF-8 failures,
symlink refusal, no-clobber setup and offline CLI analysis. Full verification:
**1242 local tests passed** (36.72 seconds), **1170 passed** in a clean staged
publication-tree export with isolated HOME (40.29 seconds), using the existing
venv. The 72-test difference remains the pre-existing local-only set. Bash syntax
and diff checks passed.

The writer inventory is [STATE_WRITERS_2026-10-03.md](STATE_WRITERS_2026-10-03.md).
It identifies unclosed form/chat whole-snapshot workflows, cookie session
ownership/durability, JSONL journals, diagnostics and log/shell handling. General
atomicity, state/resume and semantic resume-analysis coverage remain open. These
are per-file guarantees, not multi-file rollback or exactly-once processing;
post-publication directory-fsync failure can leave the new complete file visible.
Already running old code and external editors do not acquire the new advisory
env lock automatically. No production process restart/signal, provider/Telegram
request, real HH submission, runtime file edit or OSINT change was made.

### Follow-up: synchronous Google Form drafts and preview mutations (2026-10-03)

Manual answers and successor markers now mutate protected JSON under per-file
locks. Preview `remember` is a transactional update, not a separate load/save.
A short shared `.google_form_workflow.lock` coordinates preview eligibility with
edits: workflow first, then one JSON sidecar at a time. All work under the lock
is synchronous and contains no browser/model/Telegram await.

JSON/UTF-8/collection corruption stays in place and blocks repeated reads and
mutations; missing files still support initial setup. Expected collection and
entry shapes are validated without claiming exhaustive field semantics. The
old test accepting an empty replacement for malformed `items` was deliberately
updated to require preservation. Read-only compatibility checks passed for two
existing preview files and one manual-edits file; no records were exposed and
no runtime files or locks were created by that check.

Answer writes preserve other answers/tokens and metadata. Eligibility is read
after waiting for the coordinator; a terminal or superseded draft cannot then
be edited. Competing successor updates retain the first successor, reject a
different one and treat an identical repeat as a no-op. Invalid/unknown/self
successors do not create edit records. `remember` cannot replace a terminal
preview detail; identical terminal details remain no-ops. Normal expiry,
question fingerprints, option selection and required-field/skip rules remain.

Initial new baseline: **18 failures / 8 passes** on the previous code. Final
coverage adds **45 tests**: four spawned writers preserve 40 independent previews
and 40 manual answers, six competing superseders retain one successor and the
existing answer, eligibility changes while waiting are respected, and
serialization/read/replace/data-fsync failures preserve old data. Private mode,
file/parent fsync, malformed schemas, no-op and unrelated metadata are covered.
Final targeted: **102 passed** (3.85 seconds); local full: **1287 passed**
(36.43 seconds); isolated staged publication-tree: **1215 passed** (37.25 seconds)
with the existing venv. The 72-test difference remains the pre-existing
local-only set. Bash/diff checks passed.

This is a synchronous persistence group, not a complete form/chat workflow fix.
`google_form_filler.submit_saved_preview` still uses a pre-await full snapshot;
recheck/edit-version approval, submission ownership/cancellation/uncertain result,
Telegram pending-form whole snapshots and HH chat writes remain open. Full
repository `save` remains an explicit whole snapshot, not a safe merge of stale
data, and the `remember` terminal guard is not a persisted submission claim.
Older running code/non-cooperating writers do not automatically share the new
coordinator. No real submit, browser launch, provider/Telegram request,
production restart/signal, runtime edit or OSINT change was made.

### Incident correction: offline smoke isolation / cookie alerts (2026-10-03)

The user's screenshot showed repeated four-source missing-cookie warnings at
13:26. Three temporary smoke-profile warning states were persisted at
13:26:38–41. The subprocess smoke helper read the real default env file and
inherited Telegram credentials while using temporary state without cookies.
Each temporary profile had its own daily cooldown. Consequently the previous
local-full-run claims of "no Telegram request" were incorrect: those smoke
subprocesses could deliver real alerts. Publication runs with an isolated HOME
did not read that env file. The known incident is not evidence that QA cookie
files disappeared; all four QA cookie files were present when checked.

Ordinary subprocess smoke now uses an allowlisted, credential-free environment,
synthetic resume/state, forced disabled sources and temporary logs. It no longer
reads/copies candidate env, cookies, resumes or production seen. An autouse
fixture refuses real IPv4/IPv6 connect/connect_ex calls in the pytest process;
this is not an OS network sandbox and does not automatically cover subprocesses.
Their isolation is tested separately. Live checks remain separate and explicit.

Cookie alerts skip disabled sources. A protected transactional attempt claim is
persisted before sending, without holding a file lock across awaits. Failure,
cancellation or uncertain completion retains a five-minute retry cooldown;
success retains the daily cooldown. Late completion cannot overwrite a newer
attempt. Corrupt cooldown JSON/schema stays in place and suppresses delivery;
persistence failures before the claim suppress delivery too. This prevents
cooperating local senders from flooding, not external exactly-once delivery.

Verification: **1311 local tests passed** (35.77 seconds), using an empty process
environment, isolated HOME and the existing browser cache for a synthetic DOM
test. The first stricter run set JOB_HUNTER_HOME externally and exposed 33 fixture
path conflicts; removing that override resolved 32. Supplying the existing
browser cache resolved the remaining missing-browser prerequisite. Added
24 regressions cover env isolation, 30 concurrent coroutines, four spawned
senders with one delivery, retry/daily cooldowns, cancellation, corruption,
write failures, original-profile completion and late-owner protection.

Clean staged publication export: **1239 passed** (39.88 seconds), with the same
isolated HOME/environment and browser-cache prerequisite. The local-only test
difference remains 72. For subsequent ordinary full runs:

```bash
task_test_home=$(mktemp -d /tmp/job-hunter-tests-XXXXXX)
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 HOME="$task_test_home" \
  PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}" \
  ./venv/bin/python -m pytest -q
```

Do not add an outer JOB_HUNTER_HOME override: profile fixtures set their own
temporary config paths. Do not source production env to run ordinary tests.

QA bot restart was explicitly requested; it was temporarily stopped during
triage and restored with --keep-pending. Runtime and code backups are private
and separate. The async form-submit/recheck group was deferred, not completed.

An explicitly authorized short live dry-run used a private copy of QA state,
cookies and resume, one page per source and no Telegram/Office delivery. Sources
returned 322 records (HH 75, SuperJob 202, Habr 25, GeekJob 20); most were already
seen, three reached evaluation and two matched. The history's `applied=2` is a
legacy dry-run simulation counter, not real applications. Production seen was
not the destination, no application/chat-send branch ran, and the probe logged
no missing-cookie, expired-session or CAPTCHA warning. This is a bounded search
check, not certification of every cookie's session or live submit behavior.
