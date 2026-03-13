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

Additional source-specific setup:

- `SUPERJOB_API_KEY` for SuperJob search
- browser login sessions for `hh.ru`, `Habr Career`, `SuperJob`

Optional extras:

- Telegram notifications
- AI Office logging/task bridge

## First Run Checklist

1. Configure env.
2. Run `./run.sh login` for `hh.ru`.
3. Run `./run.sh habr-login` for `Habr Career`.
4. Run `./run.sh superjob-login` for `SuperJob`.
5. Run `./run.sh grab-resume` to save the working resume.
6. Run `./run.sh dry-run` before `./run.sh search`.

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

