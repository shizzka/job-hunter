# Private journals, logs and diagnostics

These paths are non-authoritative append/diagnostic output, not replacement
control-state JSON. Their failures must not silently reset business state.

## Append records

Run history, Telegram debug/chat audit, analytics events and trace events use
the shared private append helper. JSON serializes before file mutation, rejects
non-finite values, and retains an existing partial tail separated from the new
record. A stable sidecar plus inode flock serializes cooperating writers and
preserves analytics' shared-reader lock. Short writes/EINTR complete in a loop.
Files are 0600; creation fsyncs the parent and durable appends fsync the file.
Analytics retains its explicit optional durability setting.

A failed partial JSON write rolls back only the current append while locks are
held. This assumes cooperating JSON writers. A file-fsync failure after a full
append can report failure with a complete record visible: automatic retries
are not exactly-once delivery. Readers skip malformed JSON/UTF-8/non-object
diagnostic lines without erasing them. Authoritative JSON readers still fail
closed. Analytics event contexts capture destination/state/home before awaits.

## Operational logs and locks

Agent/bot handlers reopen private regular files per record and follow rotation
without truncating history. Symlinks/nonregular targets are rejected. Handler
failure prints only the exception class once until recovery, not the failed
record or raw exception. Native log content is not universally PII-redacted.

Subprocess stdout uses a private append descriptor. Inherited raw stdout does
not cooperate with record locks; framing and fsync durability are best-effort,
not certified multi-process records. Plain-log write errors never truncate
potential concurrent stdout bytes. Rotation of an inherited descriptor still
needs ordinary process/rotation management. No new retention policy is imposed.

Profile PID metadata uses complete writes and fsync on the held flock inode.
That inode is never atomically replaced or unlinked: doing so would break the
actual process lock. Launch/install scripts set umask 077; unit-file publication
is private atomic text. Tests validate syntax/rendering only, not service enable.

## Images, HTML and exports

Native failure/search/resume/chat snapshots use constructor/workflow-captured
roots and unique 0700 operation directories. Screenshots are produced inside
an owned private temporary directory and publish as complete 0600 files with
file/parent fsync. They do not change the process umask across an await. HTML
is sanitized before private atomic publication; trace URLs remove userinfo and
query secrets. Form screenshot paths remain caller-chosen explicit replacements,
not unique/CAS evidence or submission approval.

Cancellation removes only the newly owned diagnostic directory. Existing
artifacts/history are never deleted by this change. Successfully captured
diagnostics retain the existing review/trace retention behavior. Screenshots
and remaining page text can still contain personal data: do not publish them.
Model-bench output uses a unique private temporary directory and atomic JSON;
the benchmark is not run as part of this verification.

Post-publication parent-fsync errors may leave the new complete artifact visible.
Per-file publication is not an atomic image+HTML transaction. Existing broad
directories are not recursively chmodded or cleaned. Already-running old code,
manual writers, general log redaction and remote delivery are outside this
cooperative boundary. Offline pytest uses synthetic HOME and an allowlisted
environment; its in-process socket guard is not OS/browser/subprocess isolation.
