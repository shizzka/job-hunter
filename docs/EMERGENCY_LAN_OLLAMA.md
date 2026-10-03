# Temporary emergency LAN Ollama fallback

Remote LAN Ollama fallback is an emergency temporary compatibility path.
The intended future is **Job Hunter → AI Gateway**, not another router or service.

The existing `ProviderSpec` / `FallbackLLMClient` chain is reused. Nothing changes
without opting in to Ollama slots. No LAN address is shipped in defaults.

## Configuration

Use the existing private `~/.job-hunter/llm-providers.env`. Back it up with mode 0600
before replacing existing cloud slots; their credentials must not be committed.
The commented block in `job-hunter.env.example` uses a loopback placeholder: replace
its two URLs with your reachable LAN `/v1` endpoint. Keep the existing cloud keys.

Recommended emergency order: `groq,groq2,ollama,ollama2` when both Groq accounts are
configured. Additional already configured providers can be placed after `ollama2`.
`LLM_PROVIDER_ORDER` is an **allowlist**, not just a sorting hint; unlisted providers
will not be called. The environment order overrides the provider-file order.

Each explicitly set `OLLAMA_FALLBACK_MODEL`, `OLLAMA2_FALLBACK_MODEL`, or
`OLLAMA3_FALLBACK_MODEL` binds
that slot to a text model, including arbitrary per-task logical model overrides.
For example, `gpt-oss:120b` becomes `qwen3-coder:30b` only in `ollama`; it becomes
`qwen3:8b` in `ollama2`, and still receives Groq's own mapping in Groq.
Same endpoint/key with different bound models remains two chain entries.
Legacy Ollama aliases without an explicit slot binding retain their previous behavior.
Emergency requests default to `reasoning_effort=none` so Qwen's hidden thinking
does not consume a short task's entire output budget; explicitly requested effort
is respected. This default is provider-local and never leaks into cloud calls.
The field is supported by [Ollama's compatibility endpoint](https://docs.ollama.com/api/openai-compatibility).

Explicit emergency slots use direct HTTP (no cloud/system proxy), no SDK retries,
a connection timeout capped at 3 seconds and a whole-completion deadline of
`OLLAMA_TIMEOUT_SECONDS` / `OLLAMA2_TIMEOUT_SECONDS` / `OLLAMA3_TIMEOUT_SECONDS`
(default 60 seconds per slot). Invalid/nonpositive/nonfinite values fall back to 60.
Model cold loading or long generation can exceed this cap: adjust only if needed,
or warm the models on the laptop. A dead laptop does not block for minutes on connect.
The existing per-requested-model fallback TTL and shorter caller deadlines remain
in force; a caller's cancellation is not swallowed or retried.

Vision-specific models, the configured CAPTCHA model, and requests containing
image/audio/video/file parts skip emergency text-only slots. Their existing cloud
mapping/flow is preserved. Image requests have a separate fallback cache from text.

429/quota/unavailable/404/network/timeout errors continue the allowed chain.
Empty/malformed emergency response envelopes use the existing non-retryable error
policy. Matcher already turns infrastructure or invalid scoring errors into
`score=None`, `deferred_unscored`; it does not permanently reject such vacancies.
No weaker safety checks or fabricated candidate facts are introduced for 8B.

To disable with one env change, set `LLM_PROVIDER_ORDER=groq,groq2` (or your previous
cloud-only allowlist). Config is loaded when the shared client is first created;
an existing process needs an explicitly approved restart to pick up changes.
Installing this patch alone does not restart a bot or run search/apply.

## Optional private quality journal

Set `OLLAMA_QUALITY_LOG_FILE=~/.job-hunter/llm-quality/ollama.jsonl` in the private
provider file to retain full local text requests/responses for quality evaluation.
`OLLAMA2_QUALITY_LOG_FILE` / `OLLAMA3_QUALITY_LOG_FILE` optionally override the shared
path. Blank/unset overrides inherit the shared path; unset/blank all three paths
to disable tracing. There is no default full-content logging.

JSONL records pair request/response/error by `call_id` and include UTC time,
provider, requested model, actual mapped model, latency and response finish/usage
metadata. Malformed envelopes are recorded before the existing safe error policy.
Transport errors record their class/status only, not raw exception content.
Cloud and skipped vision providers are not traced. Streaming chunks are not captured
(native JH text tasks use non-streaming requests).

These files **contain personal candidate data** (resume/facts/prompts/answers), not
just analytics. Keep them private and outside the repository; new journal
directories are 0700 and files/lock sidecars are 0600. Writes use the existing
locked private journal implementation. Do not upload or publish logs without
separate consent. There is no automatic rotation, so monitor disk usage and disable
the path when enough samples are collected. No header/API-key/base-URL fields are
recorded; recognizable credential strings are redacted best-effort, not a guarantee
that arbitrary secrets in user-supplied text can be detected. Failure to write the
quality journal never rejects a vacancy or changes a successful model response.

## Verification

`tests/test_emergency_ollama.py` uses synthetic providers and HTTP MockTransport.
Ordinary pytest has an existing network-denial fixture; the LAN smoke is opt-in,
not a pytest test. With the project venv and an isolated HOME/environment:

```sh
python scripts/smoke/lan_ollama.py --allow-network --base-url http://LAN-HOST:11434/v1
```

The script makes one `/models` request and one short synthetic completion per
model, through the actual fallback adapter. It sends no profile, resume, facts,
cookies, HH or Telegram data, does not load provider credentials, disables analytics
writes, uses no cloud provider, and reports only safe model/timing/status metadata.
Both replies must be nonempty, finish normally, identify the expected actual model,
and equal the synthetic requested answer. No retries or production actions occur.

## Remove after Gateway migration

Delete the emergency `ProviderSpec.text_fallback_model` / `timeout_seconds` fields,
`_is_vision_model`, `_has_multimodal_input`, `_ollama_timeout`, their slot wiring,
model-aware dedupe, direct bounded transport and guarded completion branches in
`llm_client.py`, once Gateway owns mapping/capability/timeouts. Keep legacy aliases,
cloud fallback/error policy, usage analytics and Matcher deferred semantics until
their replacement is verified. Remove this document, the emergency env example
block, `tests/test_emergency_ollama.py`, and `scripts/smoke/lan_ollama.py` together.
Also remove `ollama_quality_log.py`, `tests/test_ollama_quality_log.py`, the
`quality_log_file` field/slot wiring/call hooks and `*_QUALITY_LOG_FILE` examples
when Gateway replaces this temporary path.
