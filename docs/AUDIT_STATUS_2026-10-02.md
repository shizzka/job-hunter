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
are not fully solved by sidecar locking. Repository-wide state work, including
the manual-apply queue and other writers, still needs its own inspection/tests.
