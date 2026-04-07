# Job Hunter v0.4.0

Russian version: [README.ru.md](README.ru.md)

`Job Hunter` is a Python automation tool for searching QA/testing vacancies across multiple job boards, scoring them with an LLM, and sending auto-applications where the platform allows it.

It supports isolated user profiles, LLM-powered resume analysis, application funnels with A/B resume testing, and an interactive setup wizard — making it usable both as a personal tool and as a foundation for a multi-user service.

Current public status: `OBT` (open beta testing). Expect selector drift, captcha limits, and platform-specific edge cases.

## What It Does

- Searches vacancies from multiple sources in one run
- Deduplicates results between platforms
- Applies a fast keyword filter before calling the LLM
- Scores each vacancy against your resume
- Generates a short cover letter for relevant matches
- Sends auto-applications where supported
- Falls back to manual-review tasks and Telegram notifications when auto-apply is not possible
- Tracks application funnel: applied → viewed → pending / rejected / positive
- Supports A/B resume testing with per-variant statistics
- Analyzes your resume with an LLM and sends recommendations to Telegram
- Supports isolated user profiles for multi-user setups
- Persists `seen` vacancies, cookies, runtime status, and debug artifacts outside the repository

## Supported Sources

| Source | Search | Details | Auto-apply |
| --- | --- | --- | --- |
| `hh.ru` | Yes | Yes | Yes |
| `Habr Career` | Yes | Yes | Yes |
| `SuperJob` | Yes | Yes | Yes |
| `GeekJob` | Yes | Yes | Yes |

## How It Works

1. Collect vacancies from enabled sources.
2. Drop duplicates between sources and repeated search queries.
3. Apply a fast keyword filter to avoid wasting LLM calls on obvious noise.
4. Fetch full vacancy details.
5. Ask the LLM to score the vacancy against your resume and explain the decision.
6. If the vacancy is relevant:
   - auto-apply on supported platforms;
   - otherwise create a manual-review item and send a notification.

More detail: [Architecture](docs/ARCHITECTURE.md)

## LLM Setup

`Job Hunter` talks to the matcher through an OpenAI-compatible API. That means you can use:

- OpenAI
- Ollama Cloud / `ollama.com`
- a local `Ollama` server exposing the OpenAI-compatible `/v1` endpoint

The scoring step and cover-letter generation both use this provider.

## AI-Assisted Setup (easiest)

If you use an AI coding assistant (Claude Code, Cursor, Windsurf, etc.), just give it this prompt:

> Clone https://github.com/shizzka/job-hunter and follow the instructions in SETUP_AGENT.md — go through all the steps in order, asking me questions at each stage.

The AI will install everything, explain what Job Hunter does, help you pick an LLM provider, set up Telegram notifications, and walk you through the entire configuration interactively.

See [SETUP_AGENT.md](SETUP_AGENT.md) for the full guide.

## Quick Start

### Option A: Interactive Setup (recommended)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

mkdir -p ~/.job-hunter
cp job-hunter.env.example ~/.job-hunter/job-hunter.env
# fill in LLM_BASE_URL, JOB_HUNTER_LLM_KEY, LLM_MODEL at minimum

./run.sh setup            # interactive wizard: profile, resume, platforms
./run.sh dry-run
./run.sh search
```

The wizard walks you through search queries, resume upload, platform accounts, and optional LLM resume analysis.

### Option B: Manual Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

mkdir -p ~/.job-hunter
cp job-hunter.env.example ~/.job-hunter/job-hunter.env

./run.sh login
./run.sh habr-login
./run.sh superjob-login
./run.sh geekjob-login

./run.sh dry-run
./run.sh search
```

## Configuration

The runtime reads environment variables from:

- `JOB_HUNTER_ENV_FILE`
- or by default `~/.job-hunter/job-hunter.env`

Important variables:

- `JOB_HUNTER_LLM_KEY`: API key for your OpenAI-compatible provider
- `LLM_BASE_URL`: provider base URL
- `LLM_MODEL`: model used for scoring and cover letters
- `SUPERJOB_API_KEY`: required for SuperJob search
- `HUNTER_BOT_TOKEN`: optional Telegram bot token for notifications
- `NOTIFY_CHAT_ID`: optional Telegram chat ID for notifications
- `OFFICE_URL`: optional AI Office HTTP API base URL
- `OFFICE_DB`: optional AI Office SQLite database path
- `JOB_HUNTER_HOME`: directory for cookies, resume, seen state, runtime status, screenshots

See the full template in [job-hunter.env.example](job-hunter.env.example).

## Customizing Search Targets

The default configuration is QA-focused because that is the original use case, but the project is not limited to QA jobs.

You can change search targets in two ways:

- edit the defaults in [config.py](config.py);
- or override them from your env file without touching the code.

Source-specific env overrides:

```env
HH_SEARCH_QUERIES=QA engineer||SDET||automation tester
SUPERJOB_SEARCH_QUERIES=QA||qa engineer||sdet
HABR_SEARCH_PATHS=/vacancies/testirovschik_qa/remote||/vacancies/devops/remote
```

Notes:

- `HH_SEARCH_QUERIES` and `SUPERJOB_SEARCH_QUERIES` are free-text queries.
- `HABR_SEARCH_PATHS` uses listing paths, not free-text terms.
- Use `||` as the separator for multiple values.
- `GeekJob` currently scans the public vacancy listing and relies on the shared filter/LLM stage instead of a source-specific query list.
- `GeekJob` auto-apply requires a saved browser session from `./run.sh geekjob-login`.

### Example: Ollama Cloud

```env
LLM_BASE_URL=https://ollama.com/v1
JOB_HUNTER_LLM_KEY=your-ollama-cloud-key
LLM_MODEL=deepseek-v3.1:671b
```

### Example: local Ollama

1. Install Ollama on your machine.
2. Pull a chat-capable model.
3. Make sure the local server is running.
4. Point `Job Hunter` at the local OpenAI-compatible endpoint.

```bash
ollama pull qwen2.5:14b
ollama serve
```

```env
LLM_BASE_URL=http://127.0.0.1:11434/v1
JOB_HUNTER_LLM_KEY=ollama
LLM_MODEL=qwen2.5:14b
```

For local Ollama the API key can be any non-empty placeholder string, because the local server usually does not enforce hosted-style auth.

## Commands

```bash
# Profile management
./run.sh setup                  # interactive profile wizard
./run.sh profiles               # list all profiles
./run.sh analyze-resume         # LLM resume analysis → file + Telegram

# Login (interactive, opens browser)
./run.sh login
./run.sh superjob-login
./run.sh habr-login
./run.sh geekjob-login
./run.sh grab-resume

# Search and apply
./run.sh dry-run
./run.sh search
./run.sh check
./run.sh daemon
./run.sh stats
./run.sh digest
./run.sh analytics-backfill

# Per-source runs
./run.sh superjob-dry-run
./run.sh superjob-search
./run.sh habr-dry-run
./run.sh habr-search
./run.sh geekjob-dry-run
./run.sh geekjob-search
```

Use `--profile <name>` with any command to run under a specific profile:

```bash
./run.sh --profile john search
./run.sh --profile john stats
```

## Profiles

`Job Hunter` supports isolated user profiles. Each profile gets its own state directory, cookies, seen vacancies, analytics, and configuration.

```
~/.job-hunter/                  # default profile state
~/.job-hunter/profiles/john/    # named profile: config + state
~/.job-hunter/profiles/anna/    # another named profile
```

Profiles are protected by OS-level file locks — two daemons cannot run the same profile concurrently.

## State and Privacy

Runtime state is intentionally stored outside the repository, by default in `~/.job-hunter/`:

- cookies for browser sessions
- downloaded resume
- `seen_vacancies.json`
- `run_history.jsonl`
- `analytics_events.jsonl` / `analytics_state.json`
- `hh_resume_pipeline.json` — A/B resume test state
- runtime status
- Playwright debug screenshots and HTML dumps

That keeps the repository safe to publish while preserving personal state locally.

## Optional Integrations

Telegram notifications and AI Office integration are both optional. If you leave their env variables empty, the core search pipeline still works.

## Built-in Stats

`./run.sh stats` shows:

- cumulative processed/applied/manual/skipped counts from `seen_vacancies.json`;
- per-source breakdown (`hh.ru`, `Habr Career`, `GeekJob`, `SuperJob`);
- top action types such as `applied`, `skipped_low_score`, `manual_*`;
- the last few search runs from `run_history.jsonl`.
- rolling analytics from `analytics_events.jsonl`: queries, resume variants, and `hh` negotiation outcomes.
- application funnel: applied → viewed → pending / rejected / positive, with response and conversion rates.
- A/B resume comparison: per-variant application count, viewed, positive, rejected, response rate, conversion rate.

## Known Limitations

- `hh.ru` and `Habr Career` DOM can change and break selectors.
- `hh.ru` can trigger captcha after many consecutive auto-applications.
- `GeekJob` auto-apply depends on a saved specialist session and can fail if GeekJob changes its JSON/API flow.
- search defaults are QA-oriented until you override them in env or `config.py`.
- LLM quality depends entirely on your prompt provider, model, and resume quality.

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Publication Notes](docs/PUBLICATION.md)
- [Changelog](CHANGELOG.md)

## License

MIT. See [LICENSE](LICENSE).
