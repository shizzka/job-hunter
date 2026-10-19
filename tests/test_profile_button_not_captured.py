"""dev 19.10: «📁 Профиль: qa» was swallowed by a waiting directions/question input."""
import asyncio

import telegram_bot
from tests.test_telegram_directions import ADMIN, Harness, make_direction
import directions


def test_profile_button_is_a_menu_button():
    assert telegram_bot._is_menu_button_text("📁 Профиль: qa")
    assert not telegram_bot._is_menu_button_text("Нравится продукт")


def test_directions_screen_lets_profile_buttons_and_free_text_through(tmp_path):
    bot = Harness(tmp_path)
    home = bot.profiles["client_1"].home_dir
    make_direction(home)

    async def run():
        await bot._dispatch_direction(1, ADMIN, "/directions", "")
        assert await bot._accept_direction_input(1, ADMIN, "📁 Профиль: qa") is False
        assert await bot._accept_direction_input(1, ADMIN, "какой-то текст") is False
        assert await bot._accept_direction_input(1, ADMIN, "1") is True  # a number still chooses

    asyncio.run(run())
