# Job Hunter v0.2.0 OBT

Russian version: [README.ru.md](README.ru.md)

`Job Hunter` is a Python automation tool for searching QA/testing vacancies across multiple job boards, scoring them with an LLM, and sending auto-applications where the platform allows it.

It is designed for personal use first, but the repository is now structured so other people can clone it, configure their own accounts, and run it on their own machine.

Current public status: `OBT` (open beta testing). Expect selector drift, captcha limits, and platform-specific edge cases.

## What It Does

- Searches vacancies from multiple sources in one run
- Deduplicates results between platforms
- Applies a fast keyword filter before calling the LLM
- Scores each vacancy against your resume
- Generates a short cover letter for relevant matches
- Sends auto-applications where supported
- Falls back to manual-review tasks and Telegram notifications when auto-apply is not possible
- Persists `seen` vacancies, cookies, runtime status, and debug artifacts outside the repository

## Supported Sources

| Source | Search | Details | Auto-apply |
| --- | --- | --- | --- |
| `hh.ru` | Yes | Yes | Yes |
| `Habr Career` | Yes | Yes | Yes |
| `SuperJob` | Yes | Yes | Yes |
| `GeekJob` | Yes | Yes | Not yet |

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

## Quick Start

1. Create and activate a virtual environment.
2. Install dependencies.
3. Install the Playwright browser.
4. Create your local env file from the example.
5. Fill in your own credentials and optional integrations.
6. Log in to the platforms that require browser sessions.
7. Run a dry run first, then a real search.

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
./run.sh login
./run.sh superjob-login
./run.sh habr-login
./run.sh grab-resume

./run.sh dry-run
./run.sh search
./run.sh check
./run.sh daemon
./run.sh stats

./run.sh superjob-dry-run
./run.sh superjob-search
./run.sh habr-dry-run
./run.sh habr-search
./run.sh geekjob-dry-run
./run.sh geekjob-search
```

## State and Privacy

Runtime state is intentionally stored outside the repository, by default in `~/.job-hunter/`:

- cookies for browser sessions
- downloaded resume
- `seen_vacancies.json`
- runtime status
- Playwright debug screenshots and HTML dumps

That keeps the repository safe to publish while preserving personal state locally.

## Optional Integrations

Telegram notifications and AI Office integration are both optional. If you leave their env variables empty, the core search pipeline still works.

## Known Limitations

- `hh.ru` and `Habr Career` DOM can change and break selectors.
- `hh.ru` can trigger captcha after many consecutive auto-applications.
- `GeekJob` currently works as a manual-review source only.
- LLM quality depends entirely on your prompt provider, model, and resume quality.

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Publication Notes](docs/PUBLICATION.md)
- [Changelog](CHANGELOG.md)

## License

MIT. See [LICENSE](LICENSE).
