from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.message(Command("id"))
async def show_id(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")
