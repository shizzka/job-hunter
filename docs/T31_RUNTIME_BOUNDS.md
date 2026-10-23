# T31: runtime bounds after the 23 October review

Telegram state-lock contention now produces a short busy reply for messages
and an answer to callback queries. Callbacks also receive a chat reply, since
an earlier acknowledgement can make a second query answer fail. No handler
is replayed. Synchronous locks
in scoped bot event-loop tasks always use nonblocking flock. The inherited
budget is 0.5 seconds for `asyncio.to_thread` workers; synchronous callers
outside the bot retain their original blocking policy.

Queue drainers, active commands and guarded read workers establish their own
policy even if their caller selected zero wait. Queue HH-guard reads run in a
worker, then revalidate the queue head after the await. Progress indicators
only sleep asynchronously and call Telegram APIs; they do not acquire state
locks. Their inherited policy therefore cannot terminate a long command on a
state-lock conflict.

Callback claims contain Unix timestamps and expire after 30 days. Every bot
state write prunes old claims. Legacy booleans receive the timestamp of their
first write under the new code, persisted without renewal on later writes.
Claim admission also prunes before checking the requested claim.

`ANALYTICS_CACHE_MAX_ROWS` bounds total retained journal projection rows
(default 250000, enough for two approximately 110000-row journals). LRU
eviction applies after reads and appends, including a valid incomplete tail;
an oversized journal is returned to its reader without cache retention.
The existing eight-journal limit also remains. Journal projections and report
entries idle for 30 minutes expire on the next cache access. Readers already
using an evicted snapshot can finish without changing their results.

Offline regressions use synthetic files and real flock contention, including
polling through the real router, busy replies, successful help after release,
worker deadlines, background task policies, claim migration/expiry and report
equality after cache eviction. No external services or live profiles are used.
