"""CDP registration protocol for unit page doubles; browser behavior uses Chromium."""
from types import SimpleNamespace
from unittest.mock import AsyncMock


def fake_cdp_context():
    async def create_session(page):
        return SimpleNamespace(send=AsyncMock(return_value={'identifier':'owned-script'}), detach=AsyncMock())
    return SimpleNamespace(new_cdp_session=create_session)
