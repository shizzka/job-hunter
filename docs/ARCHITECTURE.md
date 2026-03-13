# Architecture

## Overview

`agent.py` is the orchestrator. It does not know how a specific website works internally; it delegates platform-specific behavior to source clients and keeps the shared business logic in one place.

The architecture is intentionally simple:

- source clients fetch and normalize vacancies;
- the orchestrator deduplicates and filters them;
- the matcher asks the LLM for a decision;
- the notifier and office bridge publish results.

## Main Modules

### `agent.py`

Owns the search lifecycle:

- source collection
- deduplication
- keyword pre-filter
- LLM scoring
- auto-apply/manual fallback
- runtime status updates
- summary reporting

### `hh_client.py`

Playwright-based browser automation for:

- interactive login
- vacancy search
- vacancy details
- resume download
- application flow
- invitation checks

### `habr_career_client.py`

Hybrid client:

- `aiohttp` for SSR search/detail parsing
- Playwright for login and auto-apply

### `superjob_client.py`

Hybrid client:

- API for vacancy search
- Playwright/browser session for login and auto-apply

### `geekjob_client.py`

Pure `aiohttp` + SSR HTML parsing:

- list pages
- vacancy details
- normalized output

At the moment it is `manual-only` after matching.

### `matcher.py`

OpenAI-compatible LLM layer:

- loads the resume from disk
- scores the vacancy
- extracts red flags
- generates a short cover letter

### `seen.py`

Idempotency store. It prevents repeated processing of the same vacancy across runs.

### `notifier.py`

Telegram notifications:

- search started
- application sent
- manual review required
- invitation received
- summary by source

### `office_bridge.py`

Optional integration for external visibility:

- runtime/activity logs over HTTP
- tasks via SQLite

If `OFFICE_URL` or `OFFICE_DB` is missing, these operations quietly no-op.

## Runtime Flow

### 1. Collection

Each source returns a normalized vacancy dictionary with fields like:

- `id`
- `source`
- `title`
- `company`
- `salary`
- `url`
- `snippet`
- `details`
- `location`
- `apply_mode`

This shared schema is what makes a single pipeline possible.

### 2. Deduplication

The orchestrator builds a normalized key from:

- title
- company
- location
- URL fallback

If multiple sources contain the same vacancy, the `hh.ru` version wins when available because it usually has the richest application path.

### 3. Fast Pre-filter

Before the LLM is called, a cheap keyword filter removes obvious garbage. This is important because:

- it cuts provider cost;
- it reduces runtime;
- it avoids asking the model about clearly irrelevant positions.

`SuperJob` gets a stricter title-first filter because its search results can be noisy for generic QA terms.

### 4. LLM Scoring

The matcher receives:

- your resume
- vacancy title/company/salary/snippet
- full vacancy details when available

It must return JSON with:

- `score`
- `reason`
- `should_apply`
- `red_flags`

The orchestrator still enforces a safety rule: `score < 50` always means `should_apply = false`.

### 5. Application Phase

Behavior depends on source:

- `hh.ru`: browser auto-apply when possible
- `Habr Career`: browser auto-apply with source-specific rate limiting
- `SuperJob`: browser auto-apply, manual fallback for external ATS flows
- `GeekJob`: always manual fallback

When auto-apply fails safely, the vacancy is converted into a manual-review task instead of being dropped.

### 6. Reporting

The run ends with:

- runtime status update
- optional Telegram summary
- optional AI Office logs/tasks

## State Model

Default state directory: `~/.job-hunter/`

Important files:

- `seen_vacancies.json`
- `resume.md`
- `runtime_status.json`
- `hh_cookies.json`
- `habr_cookies.json`
- `superjob_cookies.json`
- `state/debug_*.html|png`

This design keeps personal state outside the repository and makes public sharing safe.

## Source-Specific Notes

### `hh.ru`

Strongest source, but also the most fragile:

- changing DOM
- captcha risk
- questionnaires instead of one-click apply

### `Habr Career`

Fast search path because list/detail pages are SSR and parsable without a browser. Auto-apply still uses browser automation and now enforces a minimum interval between application attempts.

### `SuperJob`

Search is API-based. Apply is browser-based because practical site behavior is more reliable than the OAuth flow for this project.

### `GeekJob`

The most lightweight source in the codebase. Easy to extend further because list pages and details are stable SSR HTML.

## Extension Guide

To add a new source:

1. Create a client that returns the normalized vacancy schema.
2. Add a source toggle and config block in `config.py`.
3. Add collection logic in `agent.py`.
4. Add source labels/order for summaries.
5. Decide whether the source is:
   - auto-apply capable;
   - manual-only;
   - or hybrid.

## Failure Strategy

The project intentionally prefers a safe failure mode:

- if LLM fails, do not auto-apply blindly;
- if a site selector breaks, create manual work instead of losing a good lead;
- if optional integrations are missing, the main search flow keeps running.

