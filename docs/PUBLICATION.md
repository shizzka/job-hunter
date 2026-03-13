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

## First Release

### Tag

`v0.2.0-obt`

### Title

`v0.2.0-obt: first public open beta`

### Release Notes

First public `OBT` release of `Job Hunter`.

This build searches QA/testing vacancies across `hh.ru`, `Habr Career`, `SuperJob`, and `GeekJob`, filters and scores them with an OpenAI-compatible LLM, and sends auto-applications on platforms where browser automation is supported.

The repository is now sanitized for public use: local secrets were removed from the code path, runtime state lives outside the repo, optional integrations are env-driven, and setup/operations documentation now includes explicit `Ollama` and `ollama.com` configuration.

### Short Version

First public `OBT` build of `Job Hunter`: multi-source QA vacancy search, LLM-based matching, browser auto-apply, Telegram notifications, and public-safe configuration/docs.

## Optional Intro For README Or Post

`Job Hunter` is a personal-first automation tool that turned into a reusable multi-source job search pipeline. It is still in open beta, but it is already usable if you are comfortable configuring browser sessions, env files, and an OpenAI-compatible LLM provider such as `Ollama`.
