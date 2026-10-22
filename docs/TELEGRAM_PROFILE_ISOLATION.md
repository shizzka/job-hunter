# DEV/PROD profiles and the tester role (B7)

Set the **instance** setting `JOB_HUNTER_INSTANCE_KIND=dev` in the DEV bot's
`job-hunter-dev.env`; set `JOB_HUNTER_INSTANCE_KIND=prod` in the PROD bot's
`job-hunter.env`. `run.sh` already loads the file selected by
`JOB_HUNTER_ENV_FILE` before starting Python. When starting Python directly,
export the instance setting explicitly.

The env filename, bot username and `--profile` are not authorization signals.
An absent or unknown instance kind logs a warning and uses PROD rules. Thus
`--profile qa-dev` with an unset kind is refused, even with a dev env filename.
The owner must configure the setting when rolling out this change. This change
does not edit operational env files or restart either bot.

| Instance | Available profile names |
| --- | --- |
| `dev` | Names ending in the exact suffix `-dev` |
| `prod`, missing, unknown | Names without that suffix |

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
      "profile": "qa-dev",
      "direction": "qa",
      "enabled": true,
      "label": "QA tester"
    }
  ]
}
```

Replace the placeholder with the tester's numeric Telegram user id, and retain
existing users. No real user id is supplied or installed by this task. The
`qa` value is the stable direction name (QA is its display title), not a title
match. The owner must prepare `qa-dev` with active direction `qa` beforehand;
missing or different active directions refuse tester commands. Invalid tester
bindings are rejected rather than normalized to unrestricted users.

Tester uses the ordinary owner's commands and UI, including existing AI credit
limits. Its profile and direction are fixed: client records and persisted menu
or profile selections cannot override them. Profile switching, direction
creation/switching, admin screens and owner administration are refused.
Direction history is confined to QA; requests for all/unspecified directions
are refused. New-direction and cross-direction history buttons are omitted.
Existing QA settings and resume variants can still be managed as by that
profile's owner.

A request-local scope also restricts data access to `qa-dev`/`qa` and the tester's
own registry entry. It follows async tasks and queue invocations. Child workers
receive `JOB_HUNTER_SCOPED_PROFILE`, `JOB_HUNTER_SCOPED_DIRECTION` and
`JOB_HUNTER_SCOPED_USER` from the launcher; these are internal scope propagation
values, not alternatives to the instance kind setting. Changing QA's active
direction outside the tester session fails closed on subsequent commands.
