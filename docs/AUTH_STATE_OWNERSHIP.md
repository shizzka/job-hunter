# Auth/token and import ownership

This covers native SuperJob API tokens, HH-auth/CAPTCHA mailboxes and the native
HH resume import. Browser cookies have their separate session documents. It is
not certification of auth freshness, account-wide submission ownership,
injected callbacks or exactly-once external requests.

## Subsequent compatibility review

`aef612b` adds explicit callback/repository validation, exact challenge guards
and native import account assertions; see `ACCOUNT_SUBMISSION_SAFETY.md`.
Unbound synthetic adapters remain supported in isolated tests but cannot publish
imports into real profiles. This closes the bounded compatibility workflow
review, not remote identity certification or arbitrary callback provenance.

## SuperJob tokens

The client captures absolute auth destination and API configuration at creation.
Protected reads validate JSON/schema and retain corruption instead of treating
it as empty state. A file revision change revokes the client: it cannot adopt a
new account silently. Unknown metadata survives a successful token refresh.

Password login and refresh reserve a durable `_token_attempt` before network
work. Only its owner can publish tokens against the expected file revision.
No file lock spans an await. Concurrent rotation attempts cannot send a second
request. OAuth token requests do not replay automatically through the direct
transport after a proxy error: the first request might already have rotated
credentials. Other API request retry semantics are unchanged.

Cancellation, invalid responses and uncertain transport/publication failures
leave an active or uncertain claim. These have no automatic expiry. Even a
failure reported after replacement may leave the new complete file visible.
Do not delete claims or retry solely because a timeout elapsed. Recovery needs
an operator to stop the relevant client, privately back up the exact auth file,
inspect account/attempt outcome, and deliberately reauthenticate or restore a
verified current account state. No automatic recovery tool is added here.

## HH auth / CAPTCHA mailboxes

Request creation, response acceptance and completion share one short workflow
lock per mailbox. Both files are validated before mutation. Responses require
the current unexpired request ID and profile; the first different answer wins.
Late cleanup can remove only its own request/response. Native waiters capture
absolute destinations before notification/browser waits, including across cwd
or configuration changes. Cancellation during auth notification still cleans
only that request. Answers are not echoed in waiter logs.

Pending and response are **two files**, not a cross-file atomic transaction.
Creation can leave a new request and an old response after interruption; fresh
ID checks prevent the old response from satisfying the new request. Corrupt
peers remain in place and block automatic mutation. Missing files still allow
bootstrap. Old running writers and non-cooperating editors are not protected
by the new lock and must not run alongside an updated conflicting workflow.

## HH resume import

Before the first browser await the importer captures profile home, env, resume,
catalog revision and input bytes. Native clients additionally bind the original
cookie file, context and cookie revision. Publication checks them again under
the cookie lock; that lock covers synchronous local publication only, never
downloads. Injected compatibility clients without cookie bindings remain
supported but are not evidence of native account ownership.

Each import has a durable owner and a unique 0700 export directory. Export
names require safe, unambiguous resume IDs; complete files publish privately.
Catalog replacement uses revision CAS; env and resume updates verify original
content under their locks. A concurrent edit/account change blocks stale data.

The catalog/env/resume/control files are **not one atomic transaction**. Failure
after the `publishing` phase can leave completed earlier files and a sticky
`uncertain` import; a preparation failure becomes `failed`. Active/publishing/
uncertain claims block retries without expiry. Before manual recovery, stop the
workflow, privately back up all these files and exports and reconcile their
versions. Never claim that every reported write failure restored old files.
Exports are retained for review; no existing production resume/export is deleted.

Synthetic tests cover stale owners, competing threads/processes, cancellation,
corruption, disk failures and destination changes. They do not establish live
login, token validity, remote delivery or the provenance of an existing HH chat.
