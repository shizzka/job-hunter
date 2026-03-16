# Publication Notes

This file contains ready-to-paste text for the first public GitHub release.

## Repository Name

`job-hunter`

## GitHub About

Use one of these short descriptions in the repository sidebar.

### Option A

LLM-assisted QA job search automation across hh.ru, Habr Career, SuperJob, and GeekJob with auto-apply, Telegram notifications, and manual-review fallback.

### Option B

Open beta tool for collecting QA vacancies from multiple job boards, scoring them against your resume, and auto-applying where browser automation is supported.

## Suggested Topics

`python`
`playwright`
`telegram-bot`
`automation`
`job-search`
`qa`
`recruiting`
`ollama`
`openai-compatible`
`llm`
`headhunter`
`superjob`

## Releases

### v0.3.0

#### Tag

`v0.3.0`

#### Title

`v0.3.0: multi-profile, resume analysis, analytics`

#### Release Notes

Major feature release of `Job Hunter`.

- **Multi-profile system**: isolated user profiles with separate state, cookies, and configuration. OS-level file locks prevent concurrent access.
- **Interactive setup wizard**: `./run.sh setup` walks through profile creation, resume upload, platform accounts, and optional LLM resume analysis.
- **LLM resume analysis**: `./run.sh analyze-resume` runs a detailed resume review through your LLM provider and sends results to Telegram.
- **Application funnel**: tracks applied → viewed → pending / rejected / positive with response and conversion rates.
- **A/B resume testing**: per-variant statistics for hh.ru resume experiments.
- **Architecture refactoring**: extracted `search_pipeline.py`, `apply_orchestrator.py`, `filters.py`, `reporting.py`, `invitation_sync.py` — `agent.py` reduced by 500+ lines.
- **152 tests**: smoke imports, filters, deduplication, profiles, parsers.

#### Short Version

Multi-profile user isolation, interactive setup wizard, LLM resume analysis, application funnel with A/B testing, and major architecture refactoring.

### v0.2.0-obt

#### Tag

`v0.2.0-obt`

#### Title

`v0.2.0-obt: first public open beta`

#### Release Notes

First public `OBT` release of `Job Hunter`.

This build searches QA/testing vacancies across `hh.ru`, `Habr Career`, `SuperJob`, and `GeekJob`, filters and scores them with an OpenAI-compatible LLM, and sends auto-applications on platforms where browser automation is supported.

The repository is now sanitized for public use: local secrets were removed from the code path, runtime state lives outside the repo, optional integrations are env-driven, and setup/operations documentation now includes explicit `Ollama` and `ollama.com` configuration.

#### Short Version

First public `OBT` build of `Job Hunter`: multi-source QA vacancy search, LLM-based matching, browser auto-apply, Telegram notifications, and public-safe configuration/docs.

## Optional Intro For README Or Post

`Job Hunter` is a personal-first automation tool that turned into a reusable multi-source job search pipeline. With v0.3.0 it supports isolated user profiles, an interactive setup wizard, and LLM-powered resume analysis — making it usable both as a personal tool and as a foundation for a multi-user service. It is still in open beta, but it is already usable if you are comfortable configuring browser sessions, env files, and an OpenAI-compatible LLM provider such as `Ollama`.
