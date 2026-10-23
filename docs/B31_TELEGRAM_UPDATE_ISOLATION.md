# B31: Telegram foreground update isolation

## Evidence and limits

The inspected baseline is `e0b6e47`. Its `telegram_bot.py:333-339` awaits each
update, daily summary and health check directly in the polling task. There is
no update deadline or exception boundary. A slow foreground await prevents
all later updates and polls; an uncaught handler exception ends the consumer.
`telegram_app/api.py:62-76` can await Telegram's entire `retry_after` inside
that foreground handler. Proxy fallback can also spend two request deadlines.
Separately, `api.py:88-90,149-152` reuses sessions by route and omits a request
timeout: the first session's timeout silently replaces subsequent deadlines.

The exact trigger of the two reported incidents is **not established** by the
available historical evidence. In particular, a foreground stall in this loop
cannot coexist with new completed polls in the same task. Process liveness or
a runtime snapshot taken after recovery does not establish continued polling
at the incident time. No contemporaneous handler/update trace was recorded.

Read-only evidence inspected:

- Profile `qa-dev/telegram-bot.log:1737-1742`: a network error at 20:27 recovered
  at 20:28; other errors surround, but do not identify, the reported 20:54 stall.
- `telegram-bot-debug.jsonl:51`: a health alert completed at 20:52:50, without
  per-update events after it. This does not locate a stalled handler.
- `telegram-bot.log:1747-1756`: 429/502/timeout and disconnects on 23 October,
  each followed by recovery; last entry is 04:58:35. There is no recorded
  transport error or handler trace for 06:02. Ordinary successful updates did
  not previously generate log records, so silence alone proves neither a
  logging deadlock nor a change of log destination.
- Crawler `crawl-20261023T030004.794582Z/run.log:2-13`: successful menu screens
  at 06:01, missing keyboard buttons at 06:02-06:03, five unanswered commands
  and a restart recorded at 06:06:25. `errors.json:29-68` identifies unanswered
  `/help`, `/directions`, `/log`, `/raw_log`, `/hh_responses`. Missing buttons
  are crawler navigation failures, distinct from unanswered slash commands.
- The supplied runtime JSON showed recent successful polling after recovery;
  the error log ended on 19 October. Neither reconstructs the incident stack.

These logs do not justify attributing either incident to a proxy, network,
access denial, logging deadlock, or a particular Telegram API request.

## Reproduction and fix

Before implementation, all four cases in
`tests/test_telegram_update_isolation.py::test_bad_update_cannot_strand_batch_or_next_poll`
failed: an indefinitely awaited first update, `sendMessage` returning 429 with
a long `retry_after`, a handler exception, and a self-cancelled handler. The
remaining eleven updates were not processed. Three cached-session timeout
cases in `test_reused_session_honors_each_request_timeout` also failed before
fixing the API request deadline.

`telegram_app/updates.py` now supervises each foreground operation:

- Cancel foreground updates after 30 seconds, log errors/timeouts, and continue
  the same batch and subsequent polling. FIFO order and existing handlers,
  access checks, command admission and background command lifetimes remain.
  Timed-out updates are not automatically replayed.
- Log start/finish, update id, handler, duration and outcome. At 10 seconds log
  the live coroutine function chain. No message text, callback arguments, auth
  codes, request URLs or raw exception payloads enter these records.
- Bound daily summary and health-check awaits too, so maintenance cannot strand
  the consumer between polls. Owner cancellation still propagates. Signals
  cancel active foreground work before session teardown and skip the rest of
  the batch.
- Log the captured log destination and timeout on startup. HTTP calls explicitly
  set their request timeout even when the route's session already exists.

The existing endpoint test and shutdown fake now accept the explicit timeout;
this intentionally changes request construction, not their behavioral checks.

The deadlines use cooperative asyncio cancellation. The review follow-up below
also removes file-lock waits from bot tasks. Other synchronous CPU/disk work
cannot be preempted by asyncio, nor can a running `asyncio.to_thread` worker be
forcibly stopped. No such historical block was established by this evidence.

## Validation

- Baseline: four update-isolation failures and three request-timeout failures.
- First focused run: 47 passed (update isolation, polling health, API).
- Extended focused run: 9 passed (batch/poll continuation, maintenance failure,
  owner/signal cancellation, real tester routing integration).
- Combined focused verification: 187 passed, including command admission,
  profile isolation, polling lifecycle and API behavior.
- Offline smoke uses the actual polling loop, router, tester profile/direction
  scope and menu handler. The first send stalls; the next help succeeds;
  administrative/profile-switch and cross-profile callback actions remain
  denied, and the scope does not leak into maintenance or the poll owner.
- No bot restart, profile-data write, Telegram/HH request, or external submit
  was performed. Full suite is run on the committed branch with the owner's
  suite helper; its final summary is reported with the task result.

## Independent review follow-up

All three findings were accepted and addressed:

1. Incomplete command admission now rolls back its queue/history entry in a
   synchronous `finally` and reschedules draining. Cancellation at markup,
   queue-notice or callback-acknowledgement awaits cannot leave a not-ready
   entry blocking subsequent commands, even if the active command finishes
   while admission is still pending. Cleanup adds no network await.
2. Telegram logging uses one nonblocking queue capped at 1,024 records. A daemon
   worker owns all configured file/stream sinks. Queue overload increments a
   counter and emits a loss report from the worker; channel and observation
   context are captured in the producer and existing redaction remains.
   Worker file-lock attempts have a 0.1-second budget and close waits at most
   0.2 seconds. Bot run/foreground tasks use a scoped zero-wait lock policy:
   sidecar and journal-inode acquisition raises `StateLockBusy` on contention,
   without accessing protected state. The supervisor logs the error and moves
   on. This also covers analytics/audit/collection journal locks; other
   processes retain the original blocking transaction policy. Synchronous
   state operations are deliberately not replayed or left running in detached
   writer threads after cancellation.
3. Daily summaries persist confirmed delivery per date, user and profile before
   awaiting the next recipient. A persisted cursor resumes after the last
   attempted recipient, so a permanently slow recipient cannot starve later
   ones. Completion is recorded once all current recipients have succeeded.
   Tests reconstruct the bot from persisted state and cover date rollover.
   A send without a confirmed result still has the API's usual uncertain
   delivery semantics; the checkpoint does not promise remote exactly-once
   delivery across a lost acknowledgement or a process crash.

Regression evidence: three queue-cancellation cases and one partial-delivery
case failed before their fixes. Five real-flock regressions also failed on an
isolated checkout of `49bd580`: a held logging sidecar and contended state,
journal append/read and analytics inode locks. A finite independent release
keeps the baseline tests from hanging their own runner. The fixed code processes
the next update while those locks are still held, without bypassing the lock
or changing the protected state. Focused follow-up verification: 12 passed;
broader storage/logging/profile checks: 243 passed, 1 xpassed. No live bot,
profile, Telegram or HH operation was used.
