# Native Habr / GeekJob / SuperJob browser cookies

This bounded group protects cookie persistence and browser cleanup, not source
autonomy, authentication freshness or application delivery.

## Ownership and publication

Each client captures its absolute cookie destination and state directory when
constructed. Startup reads and validates the cookie snapshot/revision before
the first browser await; launch options also precede that await. List and
Playwright storage-state `cookies` wrappers are readable. Unknown cookie
metadata is retained; origins/local storage are not imported by these clients.
Missing files support initial login. Invalid JSON/UTF-8/schema stays in place
and blocks reads and writes; read permission/I/O errors propagate.

`state_store.browser_cookies.CookieRepository` uses a stable per-file sidecar,
same-open-file revision (inode/timestamps/content hash), compare-and-swap and
private atomic JSON with file/parent fsync. A stale browser cannot adopt a new
login revision by rereading the file at save time. Competing missing-file
creators also conflict. An identical snapshot is a no-op. HH keeps its typed
`HHCookieStateError`, repository API and legacy module-level write seam over
this shared storage implementation; native HH lifecycle/counter are unchanged.

`BrowserCookieSession` binds a browser context to its startup revision.
Overlapping captures use an in-memory nonce; stale or replaced contexts cannot
publish. Publication failure revokes the binding, including after replacement
when directory fsync fails: a complete new file may already be visible, and no
rollback to the previous file is promised. No file lock crosses a browser or
HTTP await.

Shutdown refuses an empty snapshot over a nonempty cache; explicit
`save_session()` can intentionally save an empty list. Neither a nonempty list
nor schema validation proves an authenticated account. A changed file requires
closing/restarting the browser; no stale binding is silently refreshed.

## Cleanup

Pending startup rejects competing start/save/stop. Partial startup and
cancellation clean captured resources without publishing incomplete cookies.
Repeated ready startup is a no-op only for a valid bound context. Shutdown has
one owner, invalidates pending saves and closes the captured browser and
Playwright even if cookies or browser close fail. Late cleanup cannot clear a
new generation's fields. Full client `stop()` closes browsers even when HTTP
close fails (Habr previously did not close its browser); it preserves a newer
HTTP transport or browser generation created during that await. Habr/GeekJob
interactive login now cleans up on save failure/cancellation as SuperJob did.
Cleanup failure may still propagate; this is not proof that an OS process has
exited after an unsuccessful browser close.

GeekJob HTTP cookie headers use the constructed client's captured cookie path.
Browser proxy logs no longer print the credential-bearing URL. Other HTTP
error logs and diagnostics are not certified by this change.

## Explicit boundaries

- Legacy synchronous `_save_cookies` is a protected explicit replacement, not
  an async ownership claim; manual/non-cooperating writers and already running
  old code are not certified by the sidecar protocol.
- SuperJob API `_auth`, token refresh and auth-file writers remain a separate
  open group. Cookie ownership does not establish token/account ownership.
- GeekJob apply-context cache, browser/API account coherence, resume facts and
  submit approval/deduplication remain separate workflow contracts.
- Existing screenshot/HTML destinations, auth callbacks/imports, JSONL append,
  shell/log privacy and general state/resume remain open.
- Tests use synthetic files, fake browsers/transports and an allowlisted
  credential-free environment. The pytest IP guard covers that process only,
  not OS or subprocess isolation. No live login/search/application/API send is
  required or claimed. Production schema checks use only the pure validator,
  without repository locks/writes or printing cookie contents.

New subprocess clients load the published code. Updating source files does
not hot-reload clients in an already running process; bot restart is a separate
deployment decision, not a prerequisite for the offline evidence above.
