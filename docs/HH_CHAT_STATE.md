# HH chat state safety

`chat_responder_state.json` lives next to the captured profile resume. The
responder uses protected, sidecar-locked per-chat transactions with private
atomic/fsync-backed writes. Invalid JSON, UTF-8, counters, collections or attempt
records remain in place and block workflows; missing files support first use.
Unknown legacy metadata stays readable. Compatibility `save_state` / repository
`save` remain explicit whole-document replacements, not safe async merge APIs.
Production `process_one` / `process_all` do not call them.

Each model/browser/notification workflow reserves an owner before awaiting.
Only one active workflow is allowed per chat, without holding a file lock across
awaits. Different chats can update independently. Completion re-reads state and
patches only the owning chat. A successful reply increments the latest persisted
counter once, and records its message ID. Completed reply attempts retain dedupe
for earlier IDs, not just the last reply. The automatic per-chat cap is checked
inside the claim transaction; manual approval retains its existing cap exemption.

Replies require the target to remain the latest incoming message. Both ID and
content/classification are compared after model work, then extracted again from
the open chat immediately before Send/quick-reply click. The durable `acting`
transition happens after that final browser await, before calling click.
Preparation failures/cancellation become `failed` and may be retried. Once click
could have happened, failure/cancellation/missing verification becomes
`uncertain`; every further responder reply in that chat is blocked. A successful
send is persisted before its informational Telegram notification, so a failed
notification cannot cause another send or lose the counter.

Preview, suspicious-message and linked-form notifications also have owner claims.
Possible delivery without a confirmed outcome is not automatically replayed.
An uncertain notification/form preview blocks that same operation, but does not
claim that an HH reply was sent. Explicit manual regeneration of a completed
preview is allowed; it cannot bypass active/uncertain attempts.

## Crash, uncertainty and rollback

Active claims do not expire. A process killed before cleanup may leave
`preparing` or `acting`, even when nothing was sent. Review is required; elapsed
time alone is not proof of non-delivery. Do not restore an old state backup to
make a send retryable. Back up the current state privately, confirm no owning
agent is running, and inspect the actual HH history/Telegram delivery before
deciding whether a claim can be resolved. There is no automatic release command
or blind replay in this change. Source rollback does not roll back external
messages; old code does not honor these new claims.

Locks coordinate updated cooperating writers only, not manual edits, older
agents or external clients. Directory-fsync failure can report an error after a
complete replacement is already visible: it is not guaranteed rollback. This is
not exactly-once HH/Telegram delivery, not a cross-file transaction with Google
Forms, and not certification of chat reply matching, factual grounding, original
application resume provenance, cookie ownership or live HH behavior.

Regression suites: `test_chat_state_transactions`,
`test_chat_workflow_transactions`, existing responder/browser/command tests.
Model, browser and notification work is synthetic; no real send is needed.
