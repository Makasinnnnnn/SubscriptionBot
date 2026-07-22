from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.bot.handlers.subscriptions import send_main_menu

router = Router()


@router.message(CommandStart())
async def start(message: Message) -> None:
    await send_main_menu(message)
