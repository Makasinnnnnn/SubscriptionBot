from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import Chat, Message

from app.bot.keyboards.subscription import main_menu_keyboard
from app.education.redirect import education_channel, legacy_education


@pytest.mark.parametrize("enabled", [False, True])
def test_education_button_can_be_restored(enabled):
    with patch(
        "app.bot.keyboards.subscription.get_settings",
        return_value=SimpleNamespace(enable_builtin_education=enabled),
    ):
        button = main_menu_keyboard().inline_keyboard[2][0]
    assert button.callback_data == ("edu:menu:-" if enabled else None)
    assert button.url == (None if enabled else "https://t.me/propmethods")


async def test_old_callback_clears_draft_and_links_channel():
    message = Message(message_id=1, date=0, chat=Chat(id=1, type="private"))
    callback = SimpleNamespace(answer=AsyncMock(), message=message)
    state = SimpleNamespace(clear=AsyncMock())
    with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
        await legacy_education(callback, state)
    callback.answer.assert_awaited_once()
    state.clear.assert_awaited_once()
    assert answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].url == (
        "https://t.me/propmethods"
    )


async def test_education_command_links_channel():
    message = SimpleNamespace(answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    await education_channel(message, state)
    state.clear.assert_awaited_once()
    assert message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].url == (
        "https://t.me/propmethods"
    )
