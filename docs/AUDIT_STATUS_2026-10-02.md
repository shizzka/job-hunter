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

The newly published helpers still contain legacy registry read-modify-write
paths and non-atomic `runtime_control.write_json_file` / PID persistence. Those
paths are not covered by the reported atomic-state fixes above and remain a
separate follow-up; do not describe the entire repository as free of state bugs.

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
