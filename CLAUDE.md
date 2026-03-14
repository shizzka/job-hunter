# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Automated job search agent: scrapes vacancies from hh.ru, SuperJob, Habr Career, and GeekJob, scores them with an LLM, and auto-applies where possible. Python 3.12, async/await throughout, Playwright for browser automation.

## Commands

```bash
# Setup
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Run (always use run.sh — it loads env vars from ~/.job-hunter/job-hunter.env)
./run.sh search          # full pipeline: search + auto-apply
./run.sh dry-run         # search without applying
./run.sh check           # check hh.ru invitations
./run.sh stats           # print statistics
./run.sh digest          # send Telegram digest
./run.sh daemon          # background mode (search every 30min, check every 60min)

# Per-source runs
./run.sh habr-dry-run    # only Habr Career
./run.sh superjob-search # only SuperJob
./run.sh geekjob-search  # only GeekJob

# Login (interactive, opens browser)
./run.sh login           # hh.ru
./run.sh habr-login
./run.sh superjob-login
./run.sh geekjob-login

# Other
./run.sh grab-resume     # download resume from hh.ru
./run.sh analytics-backfill  # migrate seen entries to analytics events
./run.sh stop            # kill daemon
```

There are no automated tests. Validation is done via `./run.sh dry-run` and import checks:

```bash
source venv/bin/activate && python3 -c "import agent"
```

## Architecture

```
agent.py (orchestrator)
  ├── hh_client.py         Playwright browser automation for hh.ru
  ├── superjob_client.py   API search + browser apply
  ├── habr_career_client.py  SSR/aiohttp search + browser apply
  ├── geekjob_client.py    SSR parsing + JSON apply
  ├── matcher.py           LLM scoring + cover letter (OpenAI-compatible API)
  ├── filters.py           Keyword pre-filter (before LLM)
  ├── seen.py              Idempotency store (JSON)
  ├── analytics.py         JSONL event logging, funnel, A/B resume stats
  ├── reporting.py         Human-readable stat formatting
  ├── notifier.py          Telegram notifications
  ├── office_bridge.py     AI Office integration (optional)
  ├── hh_resume_pipeline.py  Staged A/B resume testing for hh.ru
  └── config.py            All configuration with env var overrides
```

### Pipeline flow (do_search in agent.py)

1. **Collect** vacancies from each enabled source (each returns normalized dict schema)
2. **Deduplicate** across sources (hh.ru version preferred)
3. **Keyword filter** (filters.py) — cheap reject before LLM
4. **LLM score** (matcher.py) — evaluate_vacancy returns score 0-100, red_flags, should_apply
5. **Auto-apply** — source-specific: browser click (hh/habr/sj) or JSON POST (geekjob)
6. **Fallback** — if auto-apply fails safely → manual-review task + Telegram notification
7. **Record** — mark seen, log analytics event, send notifications

### Normalized vacancy schema

All source clients return dicts with: `id`, `source`, `source_label`, `title`, `company`, `salary`, `snippet`, `details`, `url`, `location`, `apply_mode`. Private fields prefixed with `_` (e.g., `_search_query`, `_hh_resume_variant`).

### Key design rules

- **LLM error → score=0, should_apply=False** — never auto-apply blindly
- **Broken selector → manual task** — don't lose good leads
- **Optional integrations (Telegram, AI Office) → graceful no-op** if unconfigured
- Source clients are independent; the orchestrator is source-agnostic
- The 4 `_collect_*_vacancies()` functions are intentionally NOT unified — their search APIs are genuinely different

## State

All runtime state lives outside the repo in `~/.job-hunter/` (configurable via `JOB_HUNTER_HOME`):

- `*.cookies.json` — browser sessions per platform
- `seen_vacancies.json` — processed vacancies
- `analytics_events.jsonl` / `analytics_state.json` — event log and negotiation state
- `run_history.jsonl` — search run log
- `resume.md` — user's resume
- `runtime_status.json` — current agent status
- `hh_resume_pipeline.json` — A/B resume test state
- `state/debug_*.png|html` — debug screenshots on selector failures

## Configuration

`config.py` reads env vars from `~/.job-hunter/job-hunter.env` (loaded by `run.sh`). Key patterns:

- `_env_flag(name, default)` → bool
- `_env_int(name, default)` → int
- `_env_list(name, default)` → list (separators: `||` or `\n`)

Important env vars: `JOB_HUNTER_LLM_KEY`, `LLM_BASE_URL`, `LLM_MODEL`, `SUPERJOB_API_KEY`, `HUNTER_BOT_TOKEN`, `NOTIFY_CHAT_ID`, `TELEGRAM_PROXY`, `HH_PROXY`, `HEADLESS`.

## Adding a new source

1. Create `newsource_client.py` — implement `search_vacancies()`, `get_vacancy_details()`, `apply_to_vacancy()` returning normalized schema
2. Add toggle + config to `config.py` (e.g., `NEWSOURCE_ENABLED`)
3. Add `_collect_newsource_vacancies()` in `agent.py` and wire into `do_search()`
4. Add source to `SOURCE_ORDER`, `SOURCE_LABELS`, `SOURCE_SHORT_LABELS` in `reporting.py`

## Selector debugging

When hh.ru returns 0 results, check `~/.job-hunter/state/debug_search_*.html`:

```bash
grep -oP 'data-qa="[^"]*"' ~/.job-hunter/state/debug_search_*.html | sort -u
```

Then update selectors in `hh_client.py`. Set `HEADLESS=0` in env to see the browser.

## Frontend (SaaS web layer)

Stack: Next.js 14 (App Router) + TypeScript + Tailwind CSS + shadcn/ui + Lucide icons + Recharts.

Path-specific rules in `.claude/rules/frontend.md` auto-load when editing `web/**` or `src/**` files.

Available skills:
- `/ui-component <name>` — generate a component following the design system
- `/landing-section <hero|features|pricing|cta|faq|stats>` — generate a landing page section

All user-facing text in Russian.

## Language

Code comments and docstrings are in Russian. README has both English and Russian versions. Telegram notifications and web UI are in Russian.
