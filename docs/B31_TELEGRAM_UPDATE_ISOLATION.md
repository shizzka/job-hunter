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

The deadlines use cooperative asyncio cancellation. They cannot preempt a
synchronous event-loop block or forcibly stop a thread already running through
`asyncio.to_thread`. No such historical block was established by this evidence.

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
