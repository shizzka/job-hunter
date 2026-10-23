"""T31 critic (architect fix 24.10): a busy bot-state lock neither silences polling nor kills queue cleanup.
Real flock contention on synthetic files; only the Telegram transport is mocked."""
import asyncio
import json
from pathlib import Path

from state_store.locking import lock_wait_timeout
from telegram_app import guards
from tests.test_telegram_lock_contention import held_lock
from tests.test_telegram_polling_health import bot  # noqa: F401  (fixture)


def updates(*ids):
    return [{"update_id": i, "message": {"chat": {"id": 42}, "text": "/start"}} for i in ids]


def test_busy_offset_checkpoint_keeps_polling_without_redelivery(bot):
    state = Path(bot.runtime_paths.bot_state_file)
    bot._save_state({"last_update_id": 10})
    sent = []
    batches = [updates(11, 12), updates(13), []]

    async def transport(method, payload, timeout=None):
        sent.append(payload.get("offset"))
        return batches.pop(0)

    bot._api_request = transport

    async def scenario():
        with lock_wait_timeout(0):  # the bot's event-loop policy
            with held_lock(state.with_name("." + state.name + ".lock")):
                first = await bot._get_updates(timeout=0)
                second = await bot._get_updates(timeout=0)
            third = await bot._get_updates(timeout=0)  # lock free again: the deferred checkpoint lands
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert [u["update_id"] for u in first] == [11, 12] and [u["update_id"] for u in second] == [13]
    # no offset at all is safe for Telegram (confirmed updates are never re-sent); a stale offset would not be
    assert sent[0] in (None, 11) and sent[1:] == [13, 14], "a fetched batch must never be fetched twice"
    assert third == [] and json.loads(state.read_text())["last_update_id"] == 13


def test_claim_release_survives_a_busy_lock_and_lands_later(bot):
    state = Path(bot.runtime_paths.bot_state_file)
    bot._save_state({"callback_claims": {"c1": True}})

    async def scenario():
        with lock_wait_timeout(0):
            with held_lock(state.with_name("." + state.name + ".lock"), hold_seconds=1):
                guards.release_callback_soon(bot, {"_guard_claim": "c1"})  # must not raise
                assert "c1" in json.loads(state.read_text())["callback_claims"]
            await asyncio.sleep(1.5)  # retries run on the loop after the holder released the lock

    asyncio.run(scenario())
    assert "c1" not in json.loads(state.read_text()).get("callback_claims", {})
