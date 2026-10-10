"""Terminate the owned local Chromium process without document lifecycle JS."""
import asyncio
import contextlib
import os
import select

from playwright.async_api import Error


def require_termination_capability():
    """Reject unsupported hosts before creating an HH Chromium process."""
    if not hasattr(os, 'pidfd_open'):
        raise RuntimeError('HH requires local Chromium on Linux with pidfd support')
    descriptor = os.pidfd_open(os.getpid())
    try:
        if select.select([descriptor], [], [], 0)[0]:
            raise RuntimeError('HH owned-process termination capability is unproven')
    finally:
        os.close(descriptor)


async def _crash_and_confirm(browser):
    cdp = None
    descriptor = None
    try:
        cdp = await browser.new_browser_cdp_session()
        processes = (await cdp.send('SystemInfo.getProcessInfo'))['processInfo']
        pids = [int(item['id']) for item in processes if item.get('type') == 'browser']
        if len(pids) != 1 or pids[0] <= 1:
            raise RuntimeError('Owned Chromium process identity is unproven')
        # The descriptor binds this process lifetime, not a reusable numeric
        # PID. A second reply proves the same browser is still alive after it
        # was opened; the Playwright Node-driver PID is never used.
        descriptor = os.pidfd_open(pids[0])
        verified = (await cdp.send('SystemInfo.getProcessInfo'))['processInfo']
        current = [int(item['id']) for item in verified if item.get('type') == 'browser']
        if current != pids or select.select([descriptor], [], [], 0)[0]:
            raise RuntimeError('Owned Chromium process changed during binding')
        try:
            await cdp.send('Browser.crash')
        except Error:
            # The protocol connection normally disappears before the crash
            # command can return. Disconnection alone is not process proof.
            pass
        deadline = asyncio.get_running_loop().time() + 5
        while not select.select([descriptor], [], [], 0)[0]:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError('Owned Chromium termination is not confirmed')
            await asyncio.sleep(.025)
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if cdp is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(cdp.detach(), 1)


async def await_owned_completion(operation):
    """Cancellation propagates only after the owned operation has finished."""
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(operation)
            break
        except asyncio.CancelledError as exc:
            if operation.done():
                if operation.cancelled():
                    raise
                result = operation.result()
                cancellation = cancellation or exc
                break
            cancellation = cancellation or exc
        except Exception as exc:
            if cancellation is not None:
                raise cancellation from exc
            raise
    if cancellation is not None:
        raise cancellation
    return result


async def terminate_browser(browser):
    """All callers await one confirmed abrupt exit; never use graceful close."""
    operation = getattr(browser, '_jh_termination_operation', None)
    if operation is None:
        operation = asyncio.create_task(asyncio.wait_for(_crash_and_confirm(browser), 15))
        browser._jh_termination_operation = operation
    return await await_owned_completion(operation)
