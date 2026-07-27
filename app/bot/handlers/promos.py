from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.subscription import (
    main_menu_keyboard,
    payment_providers_keyboard,
)
from app.db.session import async_session_factory
from app.services.promo_service import PromoRedemptionError, PromoService

router = Router()


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
            offer = await PromoService(session).offer(
                message.from_user.id,
                raw_code,
            )
    except PromoRedemptionError as exc:
        await message.answer(
            str(exc),
            reply_markup=main_menu_keyboard(),
        )
        return
    finally:
        await state.clear()

    price_lines = [
        f"Обычная цена: {offer.base_amount:g} {offer.currency}",
        f"Цена со скидкой: {offer.discounted_amount:g} {offer.currency}",
    ]
    if offer.base_stars and offer.discounted_stars:
        price_lines.extend(
            [
                f"Обычная цена в Stars: {offer.base_stars} XTR",
                f"Цена в Stars со скидкой: {offer.discounted_stars} XTR",
            ]
        )
    await message.answer(
        f"Промокод {offer.promo.code} принят.\n\n"
        f"Тариф: {offer.promo.plan.title}\n"
        f"Канал: {offer.promo.plan.channel.title}\n"
        f"Скидка: {offer.promo.discount_percent}%\n"
        + "\n".join(price_lines)
        + "\n\nВыберите способ оплаты:",
        reply_markup=payment_providers_keyboard(
            offer.promo.plan,
            offer.promo.id,
        ),
    )
