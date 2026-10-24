"""Generated-letter doubles keep the production provenance contract."""
from unittest.mock import AsyncMock

import matcher


def grounded_letter(text):
    return matcher._remember_cover_letter_meta(text, cover_style="", grounding_status="verified")


def grounded_cover_mock(text):
    return AsyncMock(side_effect=lambda *args, **kwargs: grounded_letter(text))
