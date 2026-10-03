# HH session and response-counter safety

## Cookies

An HH client captures absolute cookie/state paths at construction. Browser
startup captures cookies and their file revision before its first await; later
profile/config/cwd changes cannot change that client's destination. Launch
settings are also copied before browser startup waits.

Native save/shutdown captures context, binding, operation nonce and expected
cookie-file revision before awaiting the browser. Publication rechecks the
context/binding/nonce, then compares the current file revision under a stable
sidecar lock. A stale browser cannot replace a newer persisted login/cookie
snapshot. Concurrent reads within one client cannot publish an older result
after a later save. A successful save advances its binding's revision; a failed
publication revokes it rather than silently adopting a possibly newer file.
No file lock crosses a browser await.

Cookie JSON/UTF-8/schema and read-I/O failures leave the file in place, not an
empty repaired session. List files and storage-state `cookies` wrappers are
readable; unknown cookie metadata is retained. Complete writes use private mode,
file fsync, atomic replacement and parent-directory fsync. The cache is a whole
browser snapshot, not a field merge. Identical saves are no-ops. Synchronous
`_save_cookies` remains an explicit import, not an async session claim; injected
callbacks own their own storage contract.

Shutdown does not erase cached `hhtoken` auth merely because a context returns
an anonymous/empty snapshot. It still closes its captured resources. A late
shutdown cannot clear another generation's resource references. Startup errors
or cancellation clean up partial resources without saving. This does not prove
that retained cookies are unexpired or that a browser/account is authenticated.

## Counters

Before HTTP, refresh captures absolute paths and one cookie snapshot, then
issues a durable per-profile sequence in `hh_response_counter_order.json`.
That file and `hh_response_counter.json` share the existing counter sidecar.
Network work holds neither a counter nor a cookie lock.

Completion checks the cookie revision and updates the counter under short locks
in order: cookies -> counter. A changed cookie session suppresses the result.
Counter completion/delta is one locked read/mutate/write against the latest
persisted observation, retaining unrelated metadata. Newest **successful**
sequence wins; a late older result returns the already persisted snapshot and
does not rewrite it. A failed newer request does not starve an older valid
result. Duplicate completions are no-ops. Ordered HTTP refreshes do not depend
on wall-clock monotonicity; direct timestamped `save_snapshot` calls additionally
refuse older timestamps. Profile/destination mismatches fail closed.

Both JSON files retain corruption and block repeated operations; issued ticket
and publication failures do not fabricate an empty counter. Gaps from failed
requests are normal. This is not a two-file atomic transaction: the sequence
can be issued without a successful observation. A later ticket starts above
the maximum persisted issued/committed sequence. HH's active and archived pages
are still separate requests, not an atomic server-side account snapshot.

## Deployment and recovery boundary

These guards coordinate updated native writers only. Already-running old code,
external clients/manual edits ignoring sidecars, and other platforms' cookie
workflows are not certified by this group. The Telegram bot imports the counter
module in-process: publishing source alone does not update that writer. Restart
the intended QA bot with `--keep-pending` only when authorized, after checking
active work and privately backing up its state; do not start a default-profile
systemd service or force a search to test these changes.

Keep cookie backups private. Do not blindly restore old cookies/counters while
another owner is active or to replay an external action. A directory-fsync
failure may occur after a complete replacement is visible: an exception does
not guarantee old-file rollback. A revoked browser must be closed and freshly
opened on the intended profile, not rebound to an unseen session. Retained
corrupt files require explicit verified restoration, not silent repair.

Synthetic regressions cover profile/await races, stale snapshots, lifecycle
cleanup/cancellation, thread/process contention, ordering/deltas, corruption and
disk errors. No real login, HH request, search or application is needed. This
group does not close global state/resume, other cookie clients, diagnostics/
append/shell, resume provenance, factual grounding or live acceptance.
