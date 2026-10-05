# Job Hunter

**Current version:** `v0.8.1`
**Status:** v0.8.1 observability patch awaiting review/merge; v0.8.0 stabilization remains frozen.

Русская версия: [README.ru.md](README.ru.md)

Job Hunter is a job-search automation system focused on **decision quality, not application volume**. It searches vacancies, removes obvious noise, evaluates fit against the candidate's real experience, grounds generated text in candidate data, and only then decides whether an application is safe to send automatically or needs human review.

The default policy targets QA/testing roles, but search policies can be customized for other professions.

## Why it exists

Typical auto-apply:

```text
vacancy -> rewrite resume -> apply -> apply -> apply
```

Job Hunter:

```text
job boards
    ↓
search + dedupe
    ↓
deterministic filters
    ↓
LLM matcher
    ↓
candidate facts / resume / knowledge base
    ↓
decision
   ↙      ↘
skip    manual review / apply
               ↓
        safety guards
               ↓
        platform adapter
               ↓
            analytics
```

The project optimizes the quality of the application decision rather than raw application count.

## Capabilities

| Capability | Status |
| --- | --- |
| Multi-source vacancy search and dedupe | ✅ |
| LLM vacancy matching | ✅ |
| Deterministic pre-filters before LLM calls | ✅ |
| Grounded cover-letter generation | ✅ |
| Exact HH resume selection before submit | ✅ |
| hh.ru employer questionnaires | ✅ |
| Google Forms from recruiter chats with preview-before-submit | ✅ |
| Telegram control plane and human approval paths | ✅ |
| HH chat drafts / screening replies | ✅ |
| Isolated candidate profiles | ✅ |
| Application funnel, resume A/B and analytics | ✅ |
| Structured HH application traces | ✅ |
| Multi-provider LLM fallback | ✅ |
| Controlled Resume Tailoring | 🚧 next sprint |

## Supported sources

| Source | Search | Details | Auto-apply | Maturity |
| --- | --- | --- | --- | --- |
| hh.ru | ✅ | ✅ | ✅ | primary, live-tested |
| GeekJob | ✅ | ✅ | ✅ | beta, guarded |
| SuperJob | ✅ | ✅ | ✅ | beta |
| Habr Career | ✅ | ✅ | ✅ | beta, DOM-dependent |

External sites change DOM, APIs and anti-bot behavior without notice. Adapter support does not mean equal maturity across all platforms.

## Safety-first behavior

Job Hunter aims to **stop when it cannot prove an automated action is safe**.

Core rules:

- unknown candidate facts stay unknown;
- vacancy requirements are not treated as proof of candidate experience;
- inferred / weak facts are not promoted to confirmed experience;
- an ambiguous or unverified HH resume blocks submission;
- LLM/provider failures become `deferred_unscored`, not fake semantic rejection;
- stale drafts, approvals or form revisions block submit;
- profile state, cookies, resume, facts and analytics are isolated;
- risky or ambiguous external actions fall back to manual review where possible;
- private runtime artifacts stay outside the repository.

See [Architecture](docs/ARCHITECTURE.md), [Account & submission safety](docs/ACCOUNT_SUBMISSION_SAFETY.md), and [Answer grounding](docs/ANSWER_GROUNDING_AND_ANALYSIS.md).

## Quick start

### 1. Install

```bash
git clone https://github.com/shizzka/job-hunter.git
cd job-hunter

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
playwright install chromium
```

### 2. Configure

```bash
mkdir -p ~/.job-hunter
cp job-hunter.env.example ~/.job-hunter/job-hunter.env
```

At minimum configure an OpenAI-compatible LLM provider:

```env
LLM_BASE_URL=https://your-provider.example/v1
JOB_HUNTER_LLM_KEY=your-key
LLM_MODEL=your-model
```

Telegram and additional job boards are optional.

### 3. Run the setup wizard

```bash
./run.sh setup
```

### 4. Start with dry-run

```bash
./run.sh dry-run
```

### 5. Run a real search

```bash
./run.sh search
```

## AI-assisted setup

If you use a coding agent, give it:

> Clone https://github.com/shizzka/job-hunter and follow SETUP_AGENT.md step by step. Ask before any operation that logs in, submits an application, sends Telegram messages, or changes production configuration.

Full guide: [SETUP_AGENT.md](SETUP_AGENT.md).

## Main commands

```bash
# Profiles
./run.sh setup
./run.sh profiles
./run.sh --profile qa dry-run
./run.sh --profile qa search

# Login
./run.sh login
./run.sh superjob-login
./run.sh habr-login
./run.sh geekjob-login

# Resume and candidate facts
./run.sh grab-resume
./run.sh analyze-resume
./run.sh extract-facts

# Analytics
./run.sh stats
./run.sh analytics
./run.sh digest

# HH chats
./run.sh chat-respond

# Services
./run.sh daemon
./run.sh bot
./run.sh status
```

See [Operations](docs/OPERATIONS.md) and [job-hunter.env.example](job-hunter.env.example) for the full command/config reference.

## LLM providers

Job Hunter uses OpenAI-compatible APIs and supports provider fallback chains.

Task-specific model overrides:

```env
HH_MATCHER_MODEL=
HH_COVER_LETTER_MODEL=
HH_QUESTION_MODEL=
HH_CHOICE_MODEL=
HH_FACTS_EXTRACT_MODEL=
HH_CHAT_RESPONDER_MODEL=
HH_CAPTCHA_VISION_MODEL=
```

Empty values fall back to `LLM_MODEL`.

The temporary LAN Ollama path is an emergency compatibility mechanism, not the target architecture. It is expected to disappear after Job Hunter migrates to the shared AI Gateway.

## Candidate profiles

Each profile owns its own:

```text
resume
facts
knowledge base
cookies
seen history
manual queues
analytics
runtime state
debug traces
```

Default locations:

```text
~/.job-hunter/
~/.job-hunter/profiles/<name>/
```

Named profiles must not inherit another candidate's biography, salary expectations, contacts or resume IDs.

## Candidate knowledge base

Confirmed candidate information can be stored in `knowledge/*.md`.

Before generating text, Job Hunter selects relevant sections and combines them with the current resume and structured facts. Vacancy text is treated as requirements/context, never as evidence that the candidate has a skill or experience.

## Telegram

Telegram acts as a control plane for workflows where human review is useful:

- manual-review vacancies;
- yellow-zone applications;
- chat-reply approval;
- Google Form editing;
- captcha bridge;
- search/profile controls;
- monitoring and analytics.

Risky workflows should require explicit confirmation or fail closed.

## Analytics

Job Hunter records a local event funnel:

```text
found
→ filtered
→ matched
→ applied/manual/deferred
→ viewed
→ rejected/positive
→ interview/test task/offer
```

Where available, it also records requested/selected resume metadata, apply mode, match score, provider/model metadata and local resume hashes.

The point is to measure not just application count, but filtering quality, resume performance and conversion.

## Tests and CI

The regular test suite must not perform real applications, Telegram sends or external submits.

```bash
python -m pytest -q
```

GitHub Actions runs the isolated offline regression suite. Browser regressions use synthetic pages with Chromium.

Live acceptance checks are separate and explicitly authorized.

## State and privacy

Runtime state lives outside Git and may include:

- cookies and auth state;
- resume / facts / knowledge;
- `seen_vacancies.json`;
- analytics journals;
- queues and approval state;
- screenshots / HTML traces;
- Google Form previews;
- chat state.

Runtime artifacts can contain personal data. Review and redact them before attaching traces to public issues.

## Known limitations

- DOM drift can break hh.ru, Habr Career and other browser integrations.
- CAPTCHA and anti-bot handling are not guaranteed.
- LLM grounding reduces hallucination risk but cannot mathematically prove truth.
- Existing HH responses do not always reveal which resume was originally submitted.
- Exactly-once delivery cannot always be proven for external actions.
- Non-HH integrations have less live coverage than the primary HH workflow.
- The offline audit did not certify live third-party platform compatibility.

## Development status

Current release path:

```text
v0.8.0 stabilization frozen
→ real soak / E2E observation
```

New abstractions should solve repeated real failure modes, not exist merely because another subsystem can be invented.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Account & submission safety](docs/ACCOUNT_SUBMISSION_SAFETY.md)
- [Answer grounding](docs/ANSWER_GROUNDING_AND_ANALYSIS.md)
- [Google Forms editing](docs/GOOGLE_FORM_EDITING.md)
- [Publication notes](docs/PUBLICATION.md)
- [Changelog](CHANGELOG.md)

## License

MIT. See [LICENSE](LICENSE).
