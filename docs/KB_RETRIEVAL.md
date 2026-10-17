# Profile knowledge retrieval

`KB_RETRIEVAL_ENABLED=1` (default) replaces generative section selection with
local BM25 in cover letters, employer chat and HH/Google questionnaires.
`0` restores legacy LLM selection/truncation. No dependencies or services are
needed to build/query the index.

The active profile root is derived from `config.RESUME_FILE`. Sources are all
`knowledge/*.md`, confirmed values and constraints in `facts.json`, and confirmed
entries in `candidate_interview.json`. Unconfirmed/inferred/legacy JSON fields
are not indexed. Evaluation labels and synonym configuration are not facts.
Symlinked knowledge sources are rejected rather than following another profile.

## Selection

1. Split Markdown by headings, carrying the heading hierarchy into each chunk.
   Tagged list items are separate chunks even when line breaks are missing.
   Plain numbered topical labels in flattened files provide heading context.
2. Split long nonmandatory bodies (including JSON interview entries) by
   paragraphs/whitespace into at most 1100-character bodies. Keep restrictions
   whole. Preserve original spelling in the rendered context and evidence.
3. Normalize search tokens: lowercase, `ё` to `е`, light Russian suffix removal,
   built-in RU/EN aliases. Preserve tool tokens such as `CI/CD`, `C++` and `3D`.
4. Reserve all `[LIMIT]`/`[DO_NOT_CLAIM]` chunks, explicitly named `LIMIT`,
   `LIMITS`, `DO_NOT_CLAIM`/`FORBIDDEN_CLAIMS` sections (including descendants),
   dedicated constraint files and structured constraints. Words such as
   «ограничения», «границы» or «запреты» in Markdown prose/headings do not mark
   a section mandatory. Horizontal rules end tagged items even in flattened
   Markdown. Interview boundary topics «границы»/«ограничения» remain explicit
   structured constraints; embedded boundary tags are always reserved as well.
5. Deduplicate mandatory items across `candidate_facts.md`, `facts.json` and
   interview copies by Unicode NFKC, case and whitespace, ignoring the item tag.
   Prefer Markdown spelling. Substring matches and semantic similarity never
   remove a restriction. Render a shared mandatory section label only once.
6. Keep up to the first 1500 body characters of each canonical profile note
   mandatory. Its remainder participates in normal retrieval; explicitly tagged
   boundaries anywhere in the note still stay mandatory and whole.
7. Rank the remainder by BM25 (`k1=1.2`, `b=0.75`),
   weighting heading tokens twice. Ties and zero-score fallback use source order.
   Add only whole chunks that fit the remaining character budget.

All new generation paths use 12000 characters. The legacy cover budget remains
6000. Mandatory content can exceed 6000 in existing profiles; it is never silently
cut. If mandatory content exceeds the requested budget, `KnowledgeBudgetError`
stops generation. The budget includes source labels and separators.

Indexes are cached per resolved profile directory and the sorted source
`mtime_ns`/size fingerprint. Native answer operations freeze the entire index
before browser/model awaits. Question-specific selection uses that frozen corpus,
then the author and checker share the exact selected `knowledge` string.
Restrictions keep priority over positive claims; theory/templates still do not
prove personal experience. Citation and claim validation remain in the existing
grounding modules.

## Optional aliases

Add `knowledge/synonyms.txt` to the profile, one group per line, for example:

```text
android = андроид
excel, эксель
```

Groups can extend built-in aliases. Comments start with `#`. Single-token aliases
are supported; multiword phrases are searched as their constituent tokens.

## Offline evaluation

Create `knowledge/kb_eval_questions.txt` **inside the profile**, never in the
repository. Each line contains a question, `→`, then JSON marker conditions:

```text
Есть Android? → {"all": ["Android"], "any": ["устройство", "телефон"]}
```

A hit requires every `all` marker and at least one `any` marker (if specified).

```sh
venv/bin/python scripts/kb_eval.py --profile qa
venv/bin/python scripts/kb_eval.py --profile qa --questions /private/questions.txt --budget 12000
```

The script compares legacy truncation at 6000, legacy truncation at the chosen
budget, and retrieval at that same budget. It uses a temporary private source
snapshot excluding evaluation labels. Output contains aggregate hit/marker recall;
it also reports mandatory chunk count, rendered characters and budget fraction.
`--details` adds ordinal IDs and booleans, with no questions, quotes or source text.
This evaluates retrieval coverage, not the correctness of generated answers.
