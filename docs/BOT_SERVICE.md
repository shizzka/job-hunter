# Telegram Bot Service

`bot-daemon` is only a background launcher. It is not a supervisor and it does not restart the bot after crashes or reboots.

For a persistent Telegram bot, run it under a user-level `systemd` service and let `systemd` handle restarts.

## Install the user service

From the repository root:

```bash
./scripts/install_job_hunter_bot_user_service.sh --enable-now
```

This installs `job-hunter-bot.service` into:

```bash
~/.config/systemd/user/job-hunter-bot.service
```

The service starts the bot in the foreground via:

```bash
./run.sh bot
```

This is intentional. Under `systemd`, do not use `bot-daemon` because `systemd` itself is the process supervisor.

## Common commands

```bash
systemctl --user status job-hunter-bot.service
systemctl --user restart job-hunter-bot.service
systemctl --user stop job-hunter-bot.service
journalctl --user -u job-hunter-bot.service -f
```

The bot still writes its own application log to `/tmp/job-hunter-bot.log` through the existing Python logging setup.

## Reboot behavior

If your desktop session auto-starts on login, the user service will start when that user session starts.

If you want the bot to start after reboot even without logging into the desktop session, enable `linger`:

```bash
sudo loginctl enable-linger "$USER"
```

Check the current state with:

```bash
loginctl show-user "$USER" -p Linger --value
```

## Notes

- The bot should be started without `--profile`; it manages access and profile selection internally.
- The unit template lives in `deploy/systemd/user/job-hunter-bot.service.in`.
- Re-run the install script after changing the unit template.
