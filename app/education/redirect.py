"""Channel-only replacement for the temporarily disabled interactive education."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router(name="education_channel")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


def channel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть методичку", url="https://t.me/propmethods")]
        ]
    )


@router.message(Command("education", "edu_signal"))
@router.message(CommandStart(deep_link=True), F.text.regexp(r"^/start(?:@\w+)? edu_"))
async def education_channel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Методичка доступна в канале propmethods.", reply_markup=channel_keyboard())


@router.callback_query(F.data.startswith("edu:"))
async def legacy_education(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Методичка доступна в канале propmethods.", reply_markup=channel_keyboard()
        )
