# Stabilization closing pass — 2026-10-04

Scope: existing Job Hunter, not a Gateway, Resume Tailoring or platform-parity
sprint. Source baseline: `709d83c`. Tests use synthetic state, mocked transports
and an isolated HOME; no real application/search or Telegram send in this pass.

## Closing findings

- HH final approval checks now run **after** verified-UI inspection awaits, not
  before them. Exact selected resume, approved cover readback and required
  questions remain the final verifier. The capture-phase unexpected-UI event
  barrier is retained.
- The existing inconclusive-result DOM submit retry receives the same verifier
  as the first submit. A changed resume, letter or required question blocks the
  second attempt; the unchanged positive path stays supported. No new retry.
- Matcher requires a complete (`finish_reason=stop`) primary/retry completion,
  a finite numeric score and correctly typed optional decision/reason/flags.
  Fractional/text scores remain compatible. Invalid output uses existing
  `llm_error` / `deferred_unscored`, never default-score rejection or a permanent
  seen record. Existing bounded JSON repair and deferred cooldown are retained.
- Blacklisted discovery records are counted as skipped/blacklisted, not Matcher
  rejects. They still bypass evaluation and apply and are not marked seen.

Native helper/full apply and search-path regressions are in
`tests/test_closing_safety.py`; SDK-like success fixtures explicitly carry the
normal stop reason in `test_matcher_deferred_safety.py`.

## Existing evidence reconciled, not reimplemented

- `aef612b`: injected auth compatibility boundaries, exact challenge ownership,
  native import account assertions and GeekJob account/approval/POST claims.
  `ACCOUNT_SUBMISSION_SAFETY.md`, `test_injected_auth_safety.py`,
  `test_geekjob_apply_ownership.py`.
- `2001df1`: grounded form/question/chat answers, advisory resume analysis and
  captured candidate/session snapshots. `test_answer_grounding_regressions.py`,
  `test_resume_analysis_regressions.py` and `test_matcher_deferred_safety.py`.
- `2e972d0` / `0122245`: bounded per-provider temporary Ollama text fallback,
  vision exclusion, provider/model accounting and opt-in private quality journal.
  `EMERGENCY_LAN_OLLAMA.md`, `test_emergency_ollama.py` and quality tests.
- Atomic/native writer workflow coverage is recorded in
  `STATE_WRITERS_2026-10-03.md` and the ownership documents, including form/chat,
  cookie/token/import, append/diagnostics and shell paths.
- Native one-vacancy HH E2E on `709d83c`, 2026-10-04: live search, description,
  Matcher, KB selection, grounded cover, exact configured resume verification,
  submit and confirmed new application all succeeded. Four text LLM calls used
  actual `ollama/qwen3-coder:30b`, complete responses. The temporary company
  blacklist exception was restored byte-for-byte. Private raw evidence is not
  committed. Do not repeat this application to reproduce the proof.
- GitHub offline CI for `709d83c` succeeded:
  https://github.com/shizzka/job-hunter/actions/runs/37192391038

## Acceptance still open

The code/offline audit is not a certificate for every external HH state.
Live CAPTCHA success and a genuinely appearing unexpected/profile modal remain
unverified. The ordinary resume-picker false positive was reproduced live and
fixed; it is not proof of a live CAPTCHA or unknown-modal recovery.

An existing response/chat does **not** prove which resume was originally sent.
It stays `unknown_existing_response` / manual review, not verified and not a
new submit. Live original-resume provenance remains an explicit acceptance gap.

Locks coordinate updated cooperating writers, not manual editors or old running
code. Multi-file publication is not atomic; partial publication remains uncertain.
Private logs are not universal PII redaction; external delivery is not exactly
once. These are documented boundaries, not reasons to add a new platform here.

Remote LAN Ollama fallback is an emergency temporary compatibility path.
After Job Hunter -> AI Gateway migration, remove the slot mapping/capability/
timeout/journal glue listed in `EMERGENCY_LAN_OLLAMA.md`; no new architecture.

## Verification

Targeted closing/Matcher/Ollama/native HH/generic compatibility suite:
**209 passed**. New closing regressions: **31 cases**. Exact old source
`709d83c` with native HHClient helper rebinding: **27 failed / 4 passed**;
failures reproduce the defects, positive unchanged/text-score cases stay valid.
Independent read-only review found no remaining High/Medium in this patch.
Full clean-worktree offline suite: **2135 passed** (147.84 seconds).
Backward-compatible cloud/model, generic-profile and synthetic browser tests
are included. The existing local-only tests in the deployed main tree are kept
and checked separately after the guarded fast-forward.

One preliminary full run caught a legacy success fixture missing the normal
SDK stop reason; it was corrected. An unnecessary shared prompt-fixture change
was reverted after its extra verifier call exposed a test expectation mismatch.
Both final targeted and full runs above are green on the final source.

Source-only deployment requires the QA profile lock, idle bot and no active
agent, verifies the original main SHA/clean tracked tree, snapshots private
state/config and checks equality after fast-forward. No restart, search, apply,
cookie reset or private provider-config change is part of this pass.
