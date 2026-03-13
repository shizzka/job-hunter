# Operations

## Requirements

- Python 3.11+
- Chromium via Playwright
- An OpenAI-compatible LLM provider
- Accounts on the job boards you want to automate

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Local Env Setup

```bash
mkdir -p ~/.job-hunter
cp job-hunter.env.example ~/.job-hunter/job-hunter.env
```

Fill in only the integrations you actually need.

Minimum practical setup:

- `JOB_HUNTER_LLM_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`

## Search Target Customization

The shipped defaults are QA-oriented, but they are only defaults.

You can customize source search targets either by editing [config.py](../config.py) or by overriding them in your env file:

```env
HH_SEARCH_QUERIES=QA engineer||SDET||automation tester
SUPERJOB_SEARCH_QUERIES=QA||qa engineer||sdet
HABR_SEARCH_PATHS=/vacancies/testirovschik_qa/remote||/vacancies/devops/remote
```

Rules:

- use `||` as a separator;
- `HH_SEARCH_QUERIES` and `SUPERJOB_SEARCH_QUERIES` are free-text queries;
- `HABR_SEARCH_PATHS` expects listing paths, not search phrases;
- `GeekJob` currently does not expose a source-specific query list and is filtered later in the shared pipeline.

## LLM Provider Setup

The vacancy scoring and cover-letter generation go through the provider configured by:

- `LLM_BASE_URL`
- `JOB_HUNTER_LLM_KEY`
- `LLM_MODEL`

The code uses an OpenAI-compatible client, so the provider must expose the `chat.completions` style API.

### Option A: Ollama Cloud / `ollama.com`

If you already use the hosted Ollama endpoint, configure:

```env
LLM_BASE_URL=https://ollama.com/v1
JOB_HUNTER_LLM_KEY=your-ollama-cloud-key
LLM_MODEL=deepseek-v3.1:671b
```

This is the shape used in the original local setup of this project.

### Option B: local Ollama

Install Ollama, pull a model, and run the server:

```bash
ollama pull qwen2.5:14b
ollama serve
```

Then configure:

```env
LLM_BASE_URL=http://127.0.0.1:11434/v1
JOB_HUNTER_LLM_KEY=ollama
LLM_MODEL=qwen2.5:14b
```

Notes:

- `JOB_HUNTER_LLM_KEY` can be any non-empty placeholder for local Ollama.
- The model must already exist locally via `ollama pull`.
- If you use a very small local model, vacancy scoring quality will drop noticeably.

### Option C: other hosted OpenAI-compatible providers

Any hosted provider that supports the OpenAI `chat.completions` interface should work:

```env
LLM_BASE_URL=https://api.openai.com/v1
JOB_HUNTER_LLM_KEY=your-key
LLM_MODEL=gpt-4.1-mini
```

Additional source-specific setup:

- `SUPERJOB_API_KEY` for SuperJob search
- browser login sessions for `hh.ru`, `Habr Career`, `SuperJob`, `GeekJob`

Optional extras:

- Telegram notifications
- AI Office logging/task bridge

## First Run Checklist

1. Configure env.
2. Run `./run.sh login` for `hh.ru`.
3. Run `./run.sh habr-login` for `Habr Career`.
4. Run `./run.sh superjob-login` for `SuperJob`.
5. Run `./run.sh geekjob-login` for `GeekJob`.
6. Run `./run.sh grab-resume` to save the working resume.
7. Run `./run.sh dry-run` before `./run.sh search`.

## Runtime Commands

### Full run

```bash
./run.sh search
```

### Dry run

```bash
./run.sh dry-run
```

### Single-source runs

```bash
./run.sh superjob-search
./run.sh habr-search
./run.sh geekjob-search
```

## Cron Example

```cron
0 9 * * *   cd /path/to/job-hunter && ./run.sh search >> /tmp/job-hunter.log 2>&1
0 14 * * *  cd /path/to/job-hunter && ./run.sh search >> /tmp/job-hunter.log 2>&1
0 19 * * *  cd /path/to/job-hunter && ./run.sh search >> /tmp/job-hunter.log 2>&1

0 8,10,12,15,17,20 * * * cd /path/to/job-hunter && ./run.sh check >> /tmp/job-hunter.log 2>&1
```

Adjust cadence to your own risk tolerance and captcha exposure.

## Logs

Default log file:

```bash
tail -f /tmp/job-hunter.log
```

Useful filtered view:

```bash
tail -f /tmp/job-hunter.log | rg 'Apply|manual|Score:|ERROR|WARNING'
```

## Runtime Status

Runtime status is persisted to:

- `~/.job-hunter/runtime_status.json`

That file is intended for dashboards or external bots that need a short answer like:

- before start
- running
- idle
- stopped
- current phase

## Debugging

### Browser issues

Use:

- `HEADLESS=0`
- larger `SLOW_MO`

### Selector drift

Inspect files in:

- `~/.job-hunter/state/`

Typical artifacts:

- `debug_*.png`
- `debug_*.html`

### `hh.ru` captcha

Symptoms:

- apply button not found
- unexpected redirects
- application flow suddenly stops

Mitigations:

- lower auto-apply caps
- reduce run frequency
- keep delays realistic
- use manual fallback instead of retry loops

### `Habr Career` rate limiting

`HABR_MIN_SECONDS_BETWEEN_APPLICATIONS` defaults to `10` seconds and is enforced in the orchestrator.

## Public Repository Hygiene

Files that should stay local:

- env files with secrets
- cookies
- runtime state
- logs
- screenshots
- local assistant notes

The provided `.gitignore` is set up to keep those out of version control.
