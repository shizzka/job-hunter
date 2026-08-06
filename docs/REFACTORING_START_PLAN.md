# Refactoring Start Plan

## Context

This document is the starting plan for a new refactoring session.

Current target branch for refactoring: `refactor/laconic-structure`.

Before starting structural refactoring, make sure the current behavior fixes are committed and pushed separately. At the time this plan was written, the worktree contained auth/captcha/Google Form/Telegram changes that should not be mixed with broad refactoring.

## Goal

Make Job Hunter easier to change without rewriting working browser automation.

The main problem is not individual bad functions. The project grew from a CLI script into a system with several job boards, daemon mode, Telegram control, HH auth recovery, captcha handling, chat replies, Google Forms, analytics, and multi-profile state. The refactor should reduce mixed responsibilities while preserving behavior.

## Non-Goals

- Do not redesign the product flow.
- Do not change selectors unless a refactor requires a small local adaptation.
- Do not replace Playwright automation.
- Do not change LLM prompts broadly.
- Do not migrate JSON state to a database in the first pass.
- Do not do a whole-project formatting or naming pass.

## Safety Rules

- Work only on a dedicated refactor branch.
- Keep each phase independently testable.
- Prefer moving code over rewriting code.
- Preserve public CLI commands in `run.sh`.
- Preserve state file formats unless a migration is explicitly added and tested.
- After each phase, run focused tests plus at least one dry-run or no-send operational check.
- Do not delete old helper functions until their callers are moved and tests cover the new path.

## Current Architecture Snapshot

- `run.sh` routes shell commands to Python entrypoints.
- `agent.py` is the main CLI orchestrator and command dispatcher.
- `search_pipeline.py` owns source collection, deduplication, keyword filtering, and summary data.
- `apply_orchestrator.py` dispatches applications to source clients.
- `profile.py` activates profiles and patches global `config.*`.
- `hh_client.py` contains most HH browser automation.
- `hh_chat_responder.py` handles HH chat reading, candidate detection, AI answer generation, and safety state.
- `google_form_filler.py` handles Google Form discovery, extraction, LLM answering, filling, submit verification, state, and notifications.
- `telegram_bot.py` handles Telegram API, access control, menus, callbacks, subprocess orchestration, HH auth bridge, captcha bridge, and client onboarding.
- State lives outside the repository in `~/.job-hunter/` and profile-specific dirs such as `~/.job-hunter/profiles/qa/`.

## Refactoring Phases

### Phase 0: Freeze Baseline

Objective: make sure behavior fixes are not mixed with structural refactor.

Steps:

1. Inspect `git status --short`.
2. Run focused tests for the current dirty worktree.
3. Commit/push existing behavior changes, or explicitly decide to include them as the baseline of the refactor branch.
4. Record the baseline command outputs in the session notes.

Recommended checks:

```bash
./venv/bin/pytest -q tests/test_client_hh_auth.py tests/test_captcha_solver.py tests/test_google_form_filler.py tests/test_telegram_bot_texts.py
./run.sh --profile qa status
./run.sh --profile qa bot-status
```

Exit criteria:

- Clean or intentionally committed baseline.
- Daemon and Telegram bot status understood.
- No broad refactor work started yet.

### Phase 1: Split `agent.py` Command Dispatch

Objective: make `agent.py` a thin entrypoint instead of a command hub.

Proposed structure:

```text
commands/
  __init__.py
  search.py
  auth.py
  chats.py
  google_forms.py
  resume.py
  analytics.py
  profiles.py
```

Steps:

1. Create `commands/` package.
2. Move Google Form CLI handling from `agent.py` into `commands/google_forms.py`.
3. Move chat CLI handling into `commands/chats.py`.
4. Move HH resume boost/status commands into `commands/resume.py`.
5. Leave `argparse` in `agent.py` for now.
6. Keep CLI output text stable.

Validation:

```bash
./venv/bin/pytest -q tests/test_google_form_filler.py tests/test_hh_chat_responder.py
./run.sh --profile qa google-form-preview --help
./run.sh --profile qa chat-respond-one 0
```

Notes:

- The `chat-respond-one 0` command may fail functionally, but it should fail through the same validation path, not import/runtime errors.
- Avoid touching search/apply behavior in this phase.

### Phase 2: Introduce State Repositories

Objective: stop spreading raw JSON read/write logic across feature modules.

Start with the two most volatile state files:

- `google_form_previews.json`
- `chat_responder_state.json`

Proposed structure:

```text
state_store/
  __init__.py
  json_store.py
  google_forms.py
  chat_responder.py
```

Responsibilities:

- atomic JSON load/save
- schema defaults
- bounded history trimming
- token/key helpers
- no Playwright, no Telegram, no LLM

Steps:

1. Add a small `JsonStore` helper with atomic write.
2. Move Google Form state load/save into `state_store/google_forms.py`.
3. Move chat responder seen Google Form state into `state_store/chat_responder.py`.
4. Keep old function names as wrappers during migration if useful.

Validation:

```bash
./venv/bin/pytest -q tests/test_google_form_filler.py tests/test_hh_chat_responder.py
```

Exit criteria:

- Feature behavior unchanged.
- Raw file writes for these two state files are centralized.

### Phase 3: Split `telegram_bot.py` by Responsibility

Objective: reduce the largest operational risk file.

Proposed structure:

```text
telegram_app/
  __init__.py
  api.py
  access.py
  callbacks.py
  commands.py
  subprocesses.py
  auth_bridge.py
  captcha_bridge_handlers.py
```

Keep existing `telegram_bot.py` as the executable entrypoint initially.

Steps:

1. Move low-level Telegram HTTP methods into `telegram_app/api.py`.
2. Move callback parsing/routing into `telegram_app/callbacks.py`.
3. Move subprocess command execution helpers into `telegram_app/subprocesses.py`.
4. Move HH auth message handling into `telegram_app/auth_bridge.py`.
5. Keep UI text/buttons in `telegram_bot_ui.py`.

Validation:

```bash
./venv/bin/pytest -q tests/test_telegram_bot.py tests/test_telegram_bot_texts.py tests/test_telegram_bot_profiles.py tests/test_telegram_access.py tests/test_telegram_clients.py
./run.sh --profile qa bot-status
```

Exit criteria:

- Telegram bot imports cleanly.
- Existing callback data stays backward compatible.
- Bot status command still works.

### Phase 4: Split Google Form Feature Internals

Objective: make Google Form behavior easier to audit, especially contact overrides and submit verification.

Proposed structure:

```text
google_forms/
  __init__.py
  urls.py
  extraction.py
  answering.py
  filling.py
  state.py
  notifications.py
```

Steps:

1. Move URL normalization/extraction into `google_forms/urls.py`.
2. Move question extraction helpers into `google_forms/extraction.py`.
3. Move answer preparation/contact overrides/fallbacks into `google_forms/answering.py`.
4. Move Playwright fill/next/submit helpers into `google_forms/filling.py`.
5. Keep `google_form_filler.py` as a compatibility facade until callers are updated.

Validation:

```bash
./venv/bin/pytest -q tests/test_google_form_filler.py
```

Exit criteria:

- Tests still import old public module successfully.
- Contact override tests remain close to answering logic.

### Phase 5: Split HH Client Carefully

Objective: reduce `hh_client.py` without destabilizing selectors.

Proposed structure:

```text
hh/
  __init__.py
  browser.py
  search.py
  apply.py
  forms.py
  chat.py
  resume.py
  invitations.py
```

Steps:

1. Move browser lifecycle/cookies/context helpers first.
2. Move resume status/boost into `hh/resume.py`.
3. Move chat-specific helpers used by `hh_chat_responder.py`.
4. Move apply form helpers last.

Validation:

```bash
./venv/bin/pytest -q tests/test_hh_client.py tests/test_hh_chat_responder.py tests/test_hh_guard.py
./run.sh --profile qa resume-status
```

Exit criteria:

- `HHClient` can remain as facade.
- Callers do not need to know the new internal module layout yet.

### Phase 6: Reduce Global Config Coupling

Objective: prepare for safer parallelism and easier testing.

Do this after structural splits, not before.

Target:

- introduce a `RuntimeContext` or `Settings` object for new code;
- keep `profile.activate()` compatibility for old code;
- pass context explicitly in newly extracted modules.

First candidates:

- Google Form state paths
- chat responder limits
- Telegram runtime paths

Validation:

```bash
./venv/bin/pytest -q
```

Exit criteria:

- No large behavior change.
- New modules do not add more direct `config.*` reads unless necessary.

## Suggested First New-Session Prompt

Use this as the opening prompt in the new session:

```text
We are in /home/q/job-hunter on branch refactor/laconic-structure.
Read docs/REFACTORING_START_PLAN.md, then start Phase 0 and Phase 1 only.
Do not refactor HH selectors or Telegram behavior yet.
Preserve CLI behavior and run focused tests after each extraction.
If the worktree has behavior fixes, summarize them and ask before mixing them with refactor commits.
```

## Commit Strategy

Use small commits:

1. `docs: add refactoring start plan`
2. `refactor(agent): extract google form commands`
3. `refactor(agent): extract chat commands`
4. `refactor(state): add google form state store`
5. `refactor(telegram): extract telegram api client`

Avoid commits that mix:

- selector changes and file moves;
- behavior fixes and module extraction;
- tests for one feature with refactor of another feature.

## Risk Register

- Browser selectors may silently break after moving helpers.
- Telegram callback data has a 64-byte limit and must remain compatible.
- Profile lock behavior is intentional; do not remove it while `config.*` is mutable.
- Google Form submit can duplicate real employer submissions; use preview/dry checks whenever possible.
- Captcha/auth flows are stateful and should be tested manually after structural changes.

## Recommended Test Matrix

Fast local tests:

```bash
./venv/bin/pytest -q tests/test_google_form_filler.py tests/test_hh_chat_responder.py tests/test_telegram_bot_texts.py
```

Auth/captcha tests:

```bash
./venv/bin/pytest -q tests/test_client_hh_auth.py tests/test_captcha_solver.py tests/test_hh_auth_bridge.py
```

Search/apply tests:

```bash
./venv/bin/pytest -q tests/test_search_pipeline.py tests/test_agent_search_note.py tests/test_manual_apply_queue.py tests/test_matcher_cover_letter.py
```

Before pushing a refactor phase:

```bash
./venv/bin/pytest -q
./run.sh --profile qa status
./run.sh --profile qa bot-status
```
