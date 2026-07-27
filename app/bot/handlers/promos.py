import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.exc import IntegrityError

from app.bot.keyboards.subscription import main_menu_keyboard
from app.db.session import async_session_factory
from app.services.promo_service import PromoRedemptionError, PromoService
from app.subscriptions.presentation import access_delivery_text
from app.subscriptions.service import ChannelAccessError

router = Router()
logger = logging.getLogger(__name__)


class PromoForm(StatesGroup):
    code = State()


@router.callback_query(F.data == "promo:enter")
async def promo_enter(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoForm.code)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "Введите промокод:",
            reply_markup=main_menu_keyboard(),
        )


@router.message(PromoForm.code)
async def promo_redeem(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        await state.clear()
        return
    raw_code = message.text or ""
    try:
        async with async_session_factory() as session:
            try:
                result = await PromoService(session).redeem(
                    message.from_user.id,
                    raw_code,
                    message.bot,
                )
                await session.commit()
            except (
                PromoRedemptionError,
                ChannelAccessError,
                TelegramAPIError,
                IntegrityError,
            ) as exc:
                await session.rollback()
                if isinstance(exc, PromoRedemptionError):
                    await message.answer(
                        str(exc),
                        reply_markup=main_menu_keyboard(),
                    )
                else:
                    logger.warning(
                        "Promo redemption failed telegram_id=%s error=%s",
                        message.from_user.id,
                        type(exc).__name__,
                    )
                    await message.answer(
                        "Не удалось применить промокод. Попробуйте позже.",
                        reply_markup=main_menu_keyboard(),
                    )
                return
    finally:
        await state.clear()

    await message.answer(
        "Промокод применён.\n\n"
        + access_delivery_text(
            expires_at=result.subscription.expires_at,
            primary_link=result.primary_link,
            materials_link=result.materials_link,
        ),
        reply_markup=main_menu_keyboard(),
    )
