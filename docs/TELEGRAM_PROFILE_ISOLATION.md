# DEV/PROD profiles and the tester role (B7)

Set the **instance** setting `JOB_HUNTER_INSTANCE_KIND=dev` in the DEV bot's
`job-hunter-dev.env`; set `JOB_HUNTER_INSTANCE_KIND=prod` in the PROD bot's
`job-hunter.env`. `run.sh` already loads the file selected by
`JOB_HUNTER_ENV_FILE` before starting Python. When starting Python directly,
export the instance setting explicitly.

The env filename, bot username and `--profile` are not authorization signals.
An absent or unknown instance kind refuses profile access. Thus
`--profile example-dev` with an unset kind is refused, even with a dev env filename.
The owner must configure the setting when rolling out this change. This change
does not edit operational env files or restart either bot.

| Instance | Available profile names |
| --- | --- |
| `dev` | Names ending in the exact suffix `-dev` |
| `prod` | Names without that suffix |
| Missing, unknown | None (access refused) |

The boundary lives in `profile_access.py` and is enforced by `profile.py` when
listing, loading, activating, creating or resolving a profile env path. DEV has
no `default` fallback. An empty DEV installation returns an empty profile list.
Telegram client/user lists and chat ownership discovery use the same boundary;
callbacks, commands, queue replays and worker processes cannot cross it. AI usage
screens filter profile names without resetting the shared credit balance. Shared
admin state pointing at another instance falls back to an available profile.
The shared owner sentinel `default` is displayed with an available DEV profile
in user lists; the stored owner entry stays intact.
Users with a forbidden binding receive a refusal, without entering onboarding.
Unassigned client applications have no proven DEV ownership and are visible
only in PROD. New DEV client profile names use the `-dev` suffix.

## Tester entry

The owner can add this entry to `users` in the DEV access registry selected by
`JOB_HUNTER_TELEGRAM_ACCESS_FILE` (normally `telegram-users.json`):

```json
{
  "users": [
    {
      "user_id": "<positive Telegram user id>",
      "role": "tester",
      "profile": "example-dev",
      "direction": "direction_<hex>",
      "enabled": true,
      "label": "Tester"
    }
  ]
}
```

Replace the placeholders with the tester's numeric Telegram user id and the
direction's exact stored stable `name`; retain existing users. `direction_<hex>`
is a placeholder for a generated name, not a literal value or a display title.
Use the prepared profile's actual name in `profile`: any name ending in `-dev`
is supported, subject to the DEV/PROD instance boundary. The bound direction must
exist in that profile, and must be its active direction for tester commands.
Missing profiles or directions and missing or different active directions refuse
access. A non-`-dev` profile is never a valid tester binding. Invalid tester
bindings are refused rather than converted to unrestricted users. These examples
contain no operational user ids, profile names or direction names.

Tester uses the ordinary owner's commands and UI, including existing AI credit
limits. Its profile and direction are fixed: client records and persisted menu
or profile selections cannot override them. Profile switching, direction
creation/switching, admin screens and owner administration are refused.
Direction history is confined to the bound direction; requests for all or
unspecified directions are refused. New-direction and cross-direction history
buttons are omitted.
Existing direction settings and resume variants can still be managed as by that
profile's owner.

A request-local scope also restricts data access to the entry's bound profile and
stable direction name, and the tester's own registry entry. It follows async
tasks and queue invocations. Child workers receive `JOB_HUNTER_SCOPED_PROFILE`,
`JOB_HUNTER_SCOPED_DIRECTION` and `JOB_HUNTER_SCOPED_USER` from the launcher;
these are internal scope propagation
values, not alternatives to the instance kind setting. Changing the profile's
active direction outside the tester session fails closed on subsequent commands.

## Asynchronous delivery and account observations

HH login mailbox discovery filters profile filenames before opening or expiring
prompts. All mailbox operations recheck the instance/request boundary, including
requests cached before a scope change. An admin's SMS reply cannot answer the
other bot's pending request.

Tester cannot use `/raw_log` or read legacy text logs through `/chat_log`.
Structured history remains available when saved evidence verifies ownership by
the bound direction. Daily summaries run in each recipient's principal scope;
a different active
direction prevents delivery to tester. Profile-wide diagnostic alerts are also
withheld from tester.

Worker delivery authorizes tester from the notification event's recorded
ownership, not the current active direction. Application/manual-review/invitation
notifications bind their event evidence for delivery. Notifications without
verified direction are withheld from tester, including configured fallback and
captured explicit targets. Other recipients retain the existing delivery rules.

The account-wide `/check` results are resolved against saved resume/application
and negotiation snapshot evidence before analytics, pipeline synchronization,
task creation or notification. Only observations owned by the bound direction
reach a scoped tester worker; ambiguous and unowned invitations are excluded.
A vacancy title or the current active direction does not establish ownership.
