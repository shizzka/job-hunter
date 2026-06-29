# Job Hunter v0.6.0

Russian version: [README.ru.md](README.ru.md)

`Job Hunter` is a Python automation tool for searching QA/testing vacancies across multiple job boards, scoring them with an LLM, and sending auto-applications where the platform allows it.

It supports isolated user profiles, LLM-powered resume analysis, application funnels with staged resume retries / A/B testing, an interactive setup wizard, **auto-answer for employer questionnaires (radio/checkbox/select)**, **hh.ru captcha solver (vision-LLM + Telegram-bridge)**, **Telegram-confirmed AI replies in hh.ru chats**, **manual AI applications for yellow-zone matches**, and **Google Forms filling from recruiter chat links**.

Current public status: `OBT` (open beta testing) → freeware. Expect selector drift, captcha limits, and platform-specific edge cases.

## What It Does

### Search & apply
- Searches vacancies from multiple sources in one run
- Deduplicates results between platforms
- Applies a fast keyword filter before calling the LLM
- Scores each vacancy against your resume + structured facts + knowledge base
- Generates a short cover letter for relevant matches
- Sends auto-applications where supported

### Employer questionnaires (hh.ru)
- Auto-answers post-application forms: text/textarea/number, **radio/checkbox/select** (including "Custom option" with custom text)
- Vacancy context, structured facts, relevant knowledge-base sections, and the canonical candidate profile are injected into the LLM prompt
- Required/starred fields are treated as mandatory; radio/select gets a retry-without-skip pass where the LLM must make a best guess instead of giving up
- Auto-answers are forwarded to the Telegram application notification alongside the questions

### Google Forms from recruiter chats
- Detects Google Forms links in HH chat messages, including `hh.ru/away?to=...` redirects and `forms.gle` short links
- Builds a Telegram preview with detected fields and proposed answers before submission
- Uses resume, structured facts, relevant knowledge-base sections, and candidate profile to fill fields; required fields are prioritized
- Submits only after Telegram confirmation and stores preview state outside the repository

### hh.ru captcha (hybrid solver)
- Stage 0: vision-LLM (`qwen3-vl:235b-instruct`) recognises text from the captcha image automatically
- Stage 1: if vision fails — a screenshot + an inline "🔁 Restart search" button are sent to Telegram; you type the characters as text → the bot fills the form
- Soft cooldown of 15 minutes instead of a 6-hour ban when the human timeout expires

### AI and screening chats on hh.ru
- Polls chats on `chatik.hh.ru` every 30 minutes (cron) and also runs as a search piggyback
- Detects official hh.ru bots ("ИИ-помощник", "Робот-помощник") via avatar, author labels, and self-introduction text
- Detects suspicious scripted HR screening messages that look like AI but are sent under a normal recruiter name
- Telegram can list fresh incoming chat candidates from the main menu (`Ответ ИИ в чат`), generate a one-off AI reply, and send it only after confirmation
- Candidate rows can also expose Google Form buttons when a recruiter asks to fill an external form
- Safety: max replies per chat, duplicate-message guard, cooldown between replies, and deterministic safe answers for sensitive questions such as study certificates

### Candidate knowledge base
- `profiles/<name>/knowledge/*.md` — structured documents about experience, skills, projects
- 2-pass LLM filter: for each vacancy the 5 most relevant sections are picked (e.g. for Mobile-QA — API/Charles/SQL, no 3D-printing fluff)
- Used in cover letters, questionnaire answers, AI-chat replies

### Anti-bot hygiene
- Configurable delay between HH applications (`HH_MIN_SECONDS_BETWEEN_APPLICATIONS`, default 12 seconds)
- Rolling HH auto-apply guard (`HH_AUTO_APPLY_MAX_PER_24H`, default 45 applications per 24 hours) plus per-run source caps
- `playwright-stealth` hides headless markers from hh.ru anti-bot detection

### Misc
- Falls back to manual-review tasks and Telegram notifications when auto-apply is not possible
- Yellow-zone vacancies can be sent from Telegram through a manual AI-application button; feedback buttons (`норм` / `мимо`) are stored for later tuning
- Tracks application funnel: applied → viewed → pending / rejected / positive
- Supports staged resume retries after rejection or long silence, with QA-only title guards to avoid service/support/developer roles
- Supports A/B resume testing with per-variant statistics
- Analyzes your resume with an LLM and sends recommendations to Telegram
- Supports isolated user profiles for multi-user setups
- Persists `seen`, cookies, runtime status, knowledge base, and debug artifacts outside the repository

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
   - use the next staged HH resume variant for retry candidates;
   - otherwise create a manual-review item with Telegram buttons for manual AI apply / feedback.

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
- `LLM_MODEL`: default model (used as a fallback)
- `SUPERJOB_API_KEY`: required for SuperJob search
- `HUNTER_BOT_TOKEN`: optional Telegram bot token for notifications
- `NOTIFY_CHAT_ID`: optional Telegram chat ID for notifications
- `OFFICE_URL`: optional AI Office HTTP API base URL
- `OFFICE_DB`: optional AI Office SQLite database path
- `JOB_HUNTER_HOME`: directory for cookies, resume, seen state, runtime status, screenshots
- `HH_RESUME_PIPELINE_ENABLED`: staged HH resume retry pipeline
- `HH_RESUME_RETRY_ON_SILENCE`: retry pending/viewed HH responses after a delay
- `HH_RESUME_RETRY_MAX_CANDIDATES_PER_RUN`: cap staged retry candidates per search run

### Per-task LLM models (optional)

Each task can use its own model for better speed/accuracy. Empty value falls back to `LLM_MODEL`:

- `HH_MATCHER_MODEL`: vacancy relevance scoring (recommended: `cogito-2.1:671b`)
- `HH_COVER_LETTER_MODEL`: cover letter generation
- `HH_QUESTION_MODEL`: free-text answers to hh.ru questionnaires
- `HH_CHOICE_MODEL`: radio/checkbox/select selection (recommended: `qwen3-coder:480b` — fast & accurate)
- `HH_FACTS_EXTRACT_MODEL`: structured facts extraction from resume (`./run.sh extract-facts`)
- `HH_CHAT_RESPONDER_MODEL`: AI-recruiter chat replies
- `HH_CAPTCHA_VISION_MODEL`: vision-LLM for captcha OCR (default `qwen3-vl:235b-instruct`)

See `scripts/smoke/model_bench.py` for the 6-models × 4-tasks benchmark.

### Anti-bot & captcha

- `HH_MIN_SECONDS_BETWEEN_APPLICATIONS=12`: pause between HH applies
- `HH_AUTO_APPLY_MAX_PER_24H=45`: rolling 24-hour HH apply limit
- `HH_ANTI_BOT_COOLDOWN_HOURS=6`: pause after captcha block
- `HH_CAPTCHA_VISION_RETRIES=2`: vision-OCR attempts before escalating to TG
- `HH_CAPTCHA_HUMAN_WINDOW_S=300`: human reply window in TG (then 15-min soft cooldown)

### Auto-answer & chats

- `HH_AUTO_ANSWER_SIMPLE_QUESTIONS=1`: enable auto-answer
- `HH_AUTO_ANSWER_USE_LLM=1`: use LLM for free text
- `HH_AUTO_ANSWER_MAX_QUESTIONS=10`: max form fields
- `HH_AUTO_ANSWER_SALARY_BASELINE=80000`: baseline salary (RUB)
- `HH_AUTO_ANSWER_SALARY_RULE`: free-form rule for adjusting salary per vacancy
- `HH_AUTO_ANSWER_PROFILE_NOTE`: canonical candidate profile (top priority in prompt)
- `HH_CHAT_RESPONDER_ENABLED=1`: enable AI-chat auto-reply
- `HH_CHAT_AUTOSEND=1`: actually send (0 = dry-run + preview in TG)
- `HH_CHAT_MAX_REPLIES_PER_CHAT=5`: safety limit per chat

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
./run.sh extract-facts          # LLM extracts structured facts.json from resume.md

# Login (interactive, opens browser)
./run.sh login
./run.sh superjob-login
./run.sh habr-login
./run.sh geekjob-login
./run.sh grab-resume

# Search and apply
./run.sh dry-run
./run.sh search
./run.sh fresh-search          # lightweight HH-only fresh search
./run.sh check
./run.sh daemon
./run.sh stats
./run.sh digest
./run.sh analytics-backfill

# hh.ru AI/screening chats
./run.sh chat-respond           # check chats, reply to AI assistants or notify about suspicious HR screening
./run.sh chat-respond-one <chat_id> [message_id]  # generate one approved reply preview for a specific chat
# Google Forms and yellow-zone manual AI applications are normally launched from Telegram inline buttons

# Per-source runs
./run.sh superjob-dry-run
./run.sh superjob-search
./run.sh habr-dry-run
./run.sh habr-search
./run.sh geekjob-dry-run
./run.sh geekjob-search

# Telegram bot
./run.sh bot                    # foreground (debug)
./run.sh bot-daemon             # background
```

### Cron (recommended schedule)

```
30 07,14 * * * cd /home/q/job-hunter && /usr/bin/flock -n /tmp/job-hunter-search.lock ./run.sh search >> /tmp/job-hunter.log 2>&1
00 23 * * * cd /home/q/job-hunter && /usr/bin/flock -n /tmp/job-hunter-search.lock ./run.sh search >> /tmp/job-hunter.log 2>&1
*/30 * * * * cd /home/q/job-hunter && /usr/bin/flock -n /tmp/job-hunter-search.lock ./run.sh chat-respond >> /tmp/job-hunter-chat.log 2>&1
```

Search 3 times a day + chat-respond every 30 minutes. Shared flock — `search` wins priority, `chat-respond` is skipped while search runs (and is piggybacked at the end of the search cycle anyway).

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
- `facts.json` — structured candidate facts (from `./run.sh extract-facts`)
- `knowledge/*.md` — user-managed knowledge base (about_me, qa_kb, etc.)
- `chat_responder_state.json` — last_replied_msg_id, suspicious-message notification state, and replies_count per chat
- `manual_apply_queue.json` — Telegram-confirmed yellow-zone AI application queue
- `google_form_previews.json` — saved Google Form previews awaiting Telegram submit confirmation
- `hh_guard_state.json` — apply counter + anti-bot blocks
- runtime status
- Playwright debug screenshots and HTML dumps (including `captcha_*.png` and `chat_preview_*.png`)

That keeps the repository safe to publish while preserving personal state locally.

## Candidate Knowledge Base

In `~/.job-hunter/profiles/<name>/knowledge/` you can drop `.md`/`.txt` files with structured facts about the candidate: "about me", "technical knowledge base", "experience with specific tools", etc.

When generating a cover letter / questionnaire answer / AI-chat reply, the module does a **2-pass LLM filter**: the first small call picks the 5 most relevant sections (by `## NN. Title` headers inside files), the second call uses only those sections in context. This:

- saves tokens (12 KB full KB → ~8 KB relevant);
- improves accuracy (no electrical-engineering fluff in a Mobile-QA prompt);
- gives detailed, factual answers instead of generic phrasing.

Files can be updated any time — the next run picks them up automatically.

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
- `hh.ru` can trigger captcha after many consecutive auto-applications. The hybrid solver (vision-LLM + TG-bridge) usually handles it, but it's not guaranteed.
- `GeekJob` auto-apply depends on a saved specialist session and can fail if GeekJob changes its JSON/API flow.
- Search defaults are QA-oriented until you override them in env or `config.py`.
- LLM quality depends entirely on your prompt provider, model, and resume/knowledge base.
- Suspicious HR-screening detection is heuristic. It intentionally does not auto-send to normal recruiter-looking accounts; Telegram approval is required before an answer is sent.
- Google Forms filling is best-effort and intended for ordinary recruiter questionnaires; the bot previews answers before submitting.
- Ollama Cloud has weekly quotas — if you hit it, temporarily switch keys (see `~/.job-hunter/llm-providers.env`) or use a different model.

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Publication Notes](docs/PUBLICATION.md)
- [Changelog](CHANGELOG.md)

## License

MIT. See [LICENSE](LICENSE).
