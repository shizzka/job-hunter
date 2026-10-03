# Telegram polling: recovery and diagnostic heartbeat

The 2026-10-03 QA inspection found a historical `bot_poll_error` left in
`telegram-bot-runtime.json`, despite no new poll errors for roughly 16 minutes.
Credential-free HEAD requests to the public Telegram root succeeded via curl
and aiohttp (default/IPv4/IPv6). A separate read-only `getMe` returned HTTP200,
`ok=true`, `is_bot=true`; neither identity fields nor token were printed.
No second `getUpdates` consumer, offset acknowledgement, search, application
or message-send probe was started. The exact original transport/TLS cause was
not established; this is not evidence of permanent network failure, invalid
cookies, an invalid token, or a need to disable TLS verification/add a proxy.

Previously, only startup, errors and command transitions updated bot runtime.
An empty successful poll did not clear a previous error. The loop now records
actual successful poll completion and resynchronizes idle/busy command state.
An active command stays busy on polling recovery. Recovery logs once, with no
extra Telegram notification. Normal retry timing and proxy/direct fallback
behavior are unchanged.

`polling` is diagnostic metadata in the existing private atomic runtime JSON:

- `status`: `starting`, `ok`, `error`, or `stopped`.
- `last_success_at`: timezone-aware timestamp of a published successful poll.
- `last_error_at`, `last_error_type`, `last_error_status`: safe failure history;
  an old error may remain here while current `status` is `ok`.

Startup/idle is not proof of successful polling. Successful heartbeats are
published at most once per 30 seconds, using monotonic throttling; first success
and recovery are immediate. Long polling may take longer than that interval.
Check both process liveness and age of `last_success_at` relative to configured
poll timeout/transport delays; a recent unrelated command/runtime timestamp or
successful `getMe` is not proof of a completed `getUpdates` request.
Shutdown marks polling stopped while retaining useful timestamps.

Diagnostic publication failures are reported by exception type only and do not
discard already fetched updates or stop network retries. A failed successful
heartbeat is retried rather than marked published. An observed poll failure
stays in memory even if its write fails, so recovery bypasses throttling after
a possibly visible post-replacement failure. As with other atomic writers,
directory-fsync failure does not guarantee the old complete file is still
visible. Runtime diagnostics are not authoritative delivery/offset state.

Polling errors, proxy-fallback warnings and health-check Telegram API details
use exception type plus an available HTTP status, never raw request URLs,
exception text or response payloads. This does not certify all application
logs or remove historical logs. Tokens/config/cookies remain private; ordinary
pytest tests use synthetic paths/credentials and fake network methods.

Deploy only to the intended standalone QA bot after source publication,
private state backup and active-work check. Preserve `--keep-pending`; do not
start the default-profile systemd service or reset offsets. Verify an actual
new `polling.last_success_at`, not just initial idle. Normal background bot work
may proceed independently; a snapshot comparison at startup does not imply
all state remains unchanged later. Broader append/log durability, full offline
isolation, HH live acceptance and resume/state stabilization remain separate.
