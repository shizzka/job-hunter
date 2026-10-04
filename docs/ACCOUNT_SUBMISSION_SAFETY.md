# Native account and submission safety

GeekJob captures its cookie destination, endpoint and selected resume at client
construction. API cookies are frozen before the first request; a changed cookie
revision revokes the client rather than switching accounts. A DummyCookieJar
prevents Set-Cookie from overriding the captured API identity. Authenticated
requests require the captured HTTPS origin, verified TLS and no redirects.
Cookie projection respects host/domain, path, expiry and secure attributes.
The effective GET-account and POST cookies must also match; path-scoped
credentials for different accounts cannot reuse an unchanged file revision.

The approved vacancy ID, canonical URL and SSR ID must agree. Resume selection
is exact and unambiguous, never silently the first CV when a configured CV is
missing. Browser/API ownership is checked again after transport preparation
and synchronously immediately before dispatch.

`geekjob_apply_attempts.json` beside the captured cookie file records a private
durable owner before POST. Query/fragment aliases use the same vacancy key.
Concurrent owners cannot submit together. Timeout, cancellation, malformed or
non-200 POST outcomes remain uncertain and are not automatically retried.
An active/preparing record left by a crash also needs manual reconciliation.
Do not delete/reset these records to force a live test. This is not a claim of
server-side exactly-once delivery; native HTTP success remains the source's
own response contract, not independent delivery verification.

HH cookie callbacks share native validation, context/revision checks and the
shutdown anti-erasure rule. A bound native writer must declare the original
repository; arbitrary injected callbacks are not native account proof. Unbound
synthetic adapters remain usable in isolated tests but cannot publish a resume
import into a real profile.
Native resume imports additionally compare the browser auth-cookie fingerprint
to the captured repository before remote work, after listing/download awaits
and before publication. Same-context token/account drift requires manual
review; this still does not establish the real person's candidate identity.

HH login/OTP replies are bound to the captured page, context, cookie revision,
attempt nonce and exact URL/prompt before waiting for Telegram. A different
challenge on the same page is rejected. Native helpers recheck after locator
resolution and before fill/click/Enter; filled credential values are read back
before submit. No OTP/login values are logged. DOM changes that cannot be
proven safe cause manual retry, not automatic submission to a new challenge.
These are bounded local checks, not atomic remote account certification.

Tests use synthetic cookies, fake HTTP and isolated profile state. No live
application, Telegram send or production auth mutation is needed by pytest.
