"""Single-flight HH counters; admission is per user, before any network await."""
import asyncio
import time

from telegram_app.guards import BUSY


async def snapshot(bot, user_id, profile_name, profile, *, force=False):
    import config
    import hh_response_counter
    key = (int(user_id), profile_name, str(profile.home_dir), str(profile.hh.cookies_file))
    entries = bot.__dict__.setdefault("_hh_counter_cache", {})
    entry = entries.setdefault(key, {})
    limits = bot.__dict__.setdefault("_hh_counter_admission", {})
    admission = limits.setdefault(int(user_id), {})
    now = time.monotonic()
    running = admission.get("task")
    if running is not None and not running.done():
        return entry.get("snapshot"), max(0, int((now - entry.get("updated", now)) // 60)), BUSY
    cached = entry.get("snapshot")
    age = max(0, now - entry.get("updated", now))
    if cached is not None and not force and age < 180:
        return cached, int(age // 60), ""
    if now - admission.get("attempted", float("-inf")) < 60:
        return cached, int(age // 60), ("Обновлять можно раз в минуту." if cached else entry.get("error", BUSY))
    # Include failed attempts: a broken session must not turn taps into HH traffic.
    admission["attempted"] = now

    async def refresh():
        try:
            result = await asyncio.to_thread(hh_response_counter.refresh,
                profile_name=profile_name, home_dir=profile.home_dir,
                cookies_file=profile.hh.cookies_file, base_url=config.HH_BASE_URL)
        except Exception as exc:
            entry["error"] = hh_response_counter.unavailable_text(exc)
        else:
            entry["snapshot"] = result
            entry["updated"] = time.monotonic()
            entry.pop("error", None)
        age = max(0, int((time.monotonic() - entry.get("updated", time.monotonic())) // 60))
        return entry.get("snapshot"), age, entry.get("error", "")

    task = asyncio.create_task(refresh())
    entry["task"] = admission["task"] = task
    # A foreground timeout cannot cancel the thread and reopen network admission.
    return await asyncio.shield(task)
