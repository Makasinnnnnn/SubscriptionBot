import json
import re
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.admin.service import AdminPermissionService
from app.db.enums import SubscriptionStatus
from app.db.models import Channel, Subscription, User
from app.db.models.education import EducationPreference, EducationSignal
from app.db.session import async_session_factory
from app.education.content import LESSONS, ROOT, TITLES
from app.education.keyboards import button, menu, navigation
from app.education.risk import CAPS, D, Trade, calculate, money, number
from app.education.signals import (
    signal_keyboard,
    signal_text,
    trade_from_signal,
    usable,
    validate_signal,
)
from app.education.store import save_preferences

router = Router(name="upscale_education")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


class RiskForm(StatesGroup):
    input = State()


async def get_signal(signal_id: str, telegram_id: int) -> dict | None:
    if signal_id == "-":
        return None
    async with async_session_factory() as session:
        record = await session.get(EducationSignal, signal_id)
        if record is None:
            return None
        channel_id = record.payload.get("channel_id")
        if channel_id is None:
            return record.payload if record.payload.get("demo") is True else None
        if await AdminPermissionService(session).can_manage_channel(telegram_id, channel_id):
            return record.payload
        subscription = await session.scalar(
            select(Subscription.id)
            .join(User, User.id == Subscription.user_id)
            .where(
                User.telegram_id == telegram_id,
                Subscription.channel_id == channel_id,
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > datetime.now(UTC).replace(tzinfo=None),
                Subscription.revoked_at.is_(None),
            )
        )
        return record.payload if subscription else None


@router.message(Command("education"))
async def education(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Обучение Upscale\nВыберите инструкцию. Практика — на демо-счёте.", reply_markup=menu()
    )


@router.message(CommandStart(deep_link=True), F.text.regexp(r"^/start(?:@\w+)? edu_"))
async def signal_start(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    payload = await get_signal((command.args or "")[4:], message.from_user.id)
    if payload is None:
        await message.answer("Сигнал не найден.", reply_markup=menu())
        return
    await message.answer(signal_text(payload), reply_markup=signal_keyboard(payload["id"]))


@router.callback_query(F.data.startswith("edu:menu:"))
async def education_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Обучение Upscale", reply_markup=menu(callback.data.split(":")[-1])
        )


@router.callback_query(F.data.startswith("edu:page:"))
async def lesson_page(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if not isinstance(callback.message, Message):
        return
    try:
        _, _, key, page_text, signal_id = callback.data.split(":")
        page = int(page_text)
        if key not in LESSONS or not 0 <= page < len(LESSONS[key]):
            raise ValueError
    except (ValueError, KeyError):
        await callback.message.answer("Инструкция не найдена.", reply_markup=menu())
        return
    context = ""
    if signal_id != "-":
        payload = await get_signal(signal_id, callback.from_user.id)
        context = signal_text(payload) if payload else "Контекст сигнала не найден."
        context += "\n\nНиже — общая инструкция. Учебные примеры не заменяют данные сигнала.\n\n"
    text = f"{TITLES[key]} · {page + 1}/{len(LESSONS[key])}\n\n{context}{LESSONS[key][page]}"
    try:
        await callback.message.edit_text(
            text,
            reply_markup=navigation(key, page, signal_id),
            parse_mode=None,
            link_preview_options={"is_disabled": True},
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


@router.message(Command("edu_signal"))
async def import_signal(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    async with async_session_factory() as session:
        if not await AdminPermissionService(session).can_manage_all_channels(message.from_user.id):
            await message.answer("Недостаточно прав для добавления общего сигнала.")
            return
        try:
            payload = validate_signal(json.loads(command.args or "", parse_float=D))
            if (
                payload["channel_id"] is not None
                and await session.get(Channel, payload["channel_id"]) is None
            ):
                raise ValueError("Канал не найден")
        except (ValueError, TypeError) as exc:
            await message.answer(
                f"Сигнал не сохранён: {exc}. Формат — JSON из инструкции интеграции."
            )
            return
        record = await session.get(EducationSignal, payload["id"], with_for_update=True)
        if record:
            if payload["revision"] <= record.payload["revision"]:
                await message.answer("Нужна версия выше сохранённой.")
                return
            history = record.payload.get("history", []) + [
                {key: value for key, value in record.payload.items() if key != "history"}
            ]
            payload["history"] = history[-100:]
            record.payload = payload
        else:
            session.add(EducationSignal(id=payload["id"], payload=payload))
        await session.commit()
    me = await message.bot.get_me()
    await message.answer(signal_text(payload), reply_markup=signal_keyboard(payload["id"]))
    await message.answer(
        f"Ссылка для сигнала: https://t.me/{me.username}?start=edu_{payload['id']}"
    )


PROMPTS = {
    "account_type": "Выберите тип счёта. Лимиты относятся к криптовалютам.",
    "mode": "Что рассчитать?",
    "nominal": "Номинал проп-счёта в USD (например 10000), не внесённые вами деньги.",
    "base": "База риска в USD: номинал или актуальная equity — введите выбранную сумму.",
    "risk_percent": "Планируемый риск в % выбранной базы (например 0,5).",
    "margin": "Ваш залог в USD с точностью до цента. Это обратный расчёт риска.",
    "leverage": "Плечо: Basic/Accelerated от 1 до 5; Turbo от 1 до 2.",
    "coin": "Тикер криптовалюты (например BTC). Только линейные USD-позиции.",
    "side": "Направление: LONG или SHORT.",
    "entry": "Цена входа в USD. Для Market — актуальная ожидаемая цена.",
    "stop": "Цена Stop Loss в USD.",
    "take": "Цена Take Profit в USD.",
    "fee_percent": "Оценка комиссии за одну сторону, % от объёма. Сверьте терминал. 0 — без комиссии.",
    "execution_percent": "Общий запас на спред/исполнение, % объёма. 0 — без этих расходов.",
    "funding_usd": "Запас на фандинг за удержание, USD. 0 — фандинг не учтён.",
    "equity": "Текущая equity из Upscale, USD. Можно пропустить.",
    "free": "Свободный баланс из Upscale, USD (не equity). Можно пропустить.",
    "daily_remaining": "Оставшийся дневной запас до нарушения, USD. Turbo — пропустите.",
    "total_remaining": "Оставшийся общий запас до стоп-уровня, USD. Turbo: динамический стоп.",
    "other_risk": "Дополнительный риск остальных позиций/ожидающих входов, USD. 0 — их нет.",
}
OPTIONAL = {"equity", "free", "daily_remaining", "total_remaining"}
ZERO = OPTIONAL | {"fee_percent", "execution_percent", "funding_usd", "other_risk"}


def fields(values: dict, signal: dict | None) -> list[str]:
    result = [
        "account_type",
        "mode",
        "nominal",
        "base",
        "margin" if values.get("mode") == "reverse" else "risk_percent",
        "leverage",
    ]
    if signal is None:
        result += ["coin", "side", "entry", "stop", "take"]
    return result + [
        "fee_percent",
        "execution_percent",
        "funding_usd",
        "equity",
        "free",
        "daily_remaining",
        "total_remaining",
        "other_risk",
    ]


async def prompt(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    values = data["values"]
    field = next((name for name in fields(values, data.get("signal")) if name not in values), None)
    if field is None:
        await finish(message, state, data)
        return
    await state.update_data(field=field)
    rows = []
    if field == "account_type":
        rows = [[button(name, f"edu:value:{name}")] for name in CAPS]
    elif field == "mode":
        rows = [
            [button("Залог по риску", "edu:value:forward")],
            [button("Риск по залогу", "edu:value:reverse")],
        ]
    elif field == "side":
        rows = [[button(name, f"edu:value:{name}")] for name in ("LONG", "SHORT")]
    if field in OPTIONAL:
        rows.append([button("Пропустить", "edu:value:skip")])
    saved = data.get("saved", {}).get(field)
    if saved is not None and field in {"nominal", "risk_percent", "leverage"}:
        rows.append([button(f"Использовать {saved}", f"edu:value:{saved}")])
    rows.append([button("Отмена / к инструкциям", "edu:menu:-")])
    await message.answer(PROMPTS[field], reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("edu:calc:"))
async def begin_calculation(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    signal_id = callback.data.split(":")[-1]
    signal = await get_signal(signal_id, callback.from_user.id)
    if signal_id != "-" and (signal is None or not usable(signal)):
        await callback.message.answer(
            "Сигнал отсутствует, отменён, закрыт или просрочен. Новый вход не рассчитывается.",
            reply_markup=menu(signal_id),
        )
        return
    async with async_session_factory() as session:
        record = await session.get(EducationPreference, callback.from_user.id)
        saved = record.values if record else {}
    await state.clear()
    await state.set_state(RiskForm.input)
    await state.update_data(
        values={}, saved=saved, signal=signal, signal_id=signal_id, user_id=callback.from_user.id
    )
    await prompt(callback.message, state)


async def accept(message: Message, state: FSMContext, raw: str) -> None:
    data = await state.get_data()
    field = data.get("field")
    if not field:
        return
    values = dict(data["values"])
    try:
        if field in OPTIONAL and raw in {"skip", "-"}:
            value = None
        elif field == "account_type":
            if raw not in CAPS:
                raise ValueError("Выберите тип счёта кнопкой")
            value = raw
        elif field == "mode":
            if raw not in {"forward", "reverse"}:
                raise ValueError("Выберите режим кнопкой")
            value = raw
        elif field == "coin":
            value = raw.upper().strip()
            if not re.fullmatch(r"[A-Z0-9]{1,16}", value):
                raise ValueError("Введите тикер латиницей")
        elif field == "side":
            value = raw.upper().strip()
            if value not in {"LONG", "SHORT"}:
                raise ValueError("Направление: LONG или SHORT")
        else:
            parsed = number(raw, zero=field in ZERO)
            if field == "leverage" and not 1 <= parsed <= CAPS[values["account_type"]]:
                raise ValueError("Плечо недоступно для выбранного счёта")
            if field in {"risk_percent", "fee_percent", "execution_percent"} and parsed > 100:
                raise ValueError("Процент не может превышать 100")
            if field == "margin" and parsed != parsed.quantize(D(".01")):
                raise ValueError("Залог — не более двух знаков после запятой")
            value = str(parsed)
        values[field] = value
        if field == "take" and data.get("signal") is None:
            Trade(
                values["coin"],
                values["side"],
                number(values["entry"]),
                number(values["stop"]),
                number(values["take"]),
            ).validate()
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(values=values)
    await prompt(message, state)


@router.callback_query(RiskForm.input, F.data.startswith("edu:value:"))
async def calculation_choice(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await accept(callback.message, state, callback.data.removeprefix("edu:value:"))


@router.message(RiskForm.input, F.text, ~F.text.startswith("/"))
async def calculation_text(message: Message, state: FSMContext) -> None:
    await accept(message, state, message.text)


async def finish(message: Message, state: FSMContext, data: dict) -> None:
    values = data["values"]
    signal = data.get("signal")
    if signal:
        current = await get_signal(signal["id"], data["user_id"])
        if current is None or not usable(current) or current["revision"] != signal["revision"]:
            await state.clear()
            await message.answer(
                "Сигнал изменился или истёк. Откройте его заново.",
                reply_markup=menu(data["signal_id"]),
            )
            return
        trade = trade_from_signal(current)
    else:
        trade = Trade(
            values["coin"],
            values["side"],
            number(values["entry"]),
            number(values["stop"]),
            number(values["take"]),
        )
    kwargs = {
        key: (D(value) if value is not None else None)
        for key, value in values.items()
        if key not in {"account_type", "mode", "coin", "side", "entry", "stop", "take"}
    }
    try:
        estimate = calculate(trade, account_type=values["account_type"], **kwargs)
    except ValueError as exc:
        await state.clear()
        await message.answer(
            f"Расчёт невозможен: {exc}. Начните заново с исправленными данными.",
            reply_markup=menu(data["signal_id"]),
        )
        return
    async with async_session_factory() as session:
        await save_preferences(session, data["user_id"], values)
        await session.commit()
    await state.clear()
    await message.answer(
        f"Оценка · {trade.coin} {trade.side} · {values['account_type']}\n"
        f"База риска: {values['base']} USD; номинал: {values['nominal']} USD\n"
        f"Вход {trade.entry}; SL {trade.stop}; TP {trade.take}\n"
        f"Плечо x{values['leverage']}\nЗалог: {money(estimate.margin)} USD\n"
        f"Объём: {money(estimate.notional)} USD ≈ {estimate.quantity} {trade.coin}\n"
        f"Движение до SL: −{money(estimate.movement_loss)} USD\n"
        f"Резерв издержек при SL: {money(estimate.estimated_costs)} USD\n"
        f"Убыток при SL с оценкой издержек: {money(estimate.loss)} USD "
        f"({money(estimate.risk_percent)}% базы)\n"
        f"Результат при TP с оценкой издержек: {money(estimate.profit)} USD\n"
        f"Доля номинала под залог: {money(estimate.margin_percent)}%\n\n"
        "В режиме «На сумму» комиссия может уменьшить фактическую маржу. "
        "Сверьте предварительный объём и риск терминала; не повышайте сумму автоматически.\n\n"
        + "\n".join(estimate.warnings),
        reply_markup=navigation("risk", 0, data["signal_id"]),
    )


@router.callback_query(F.data.startswith("edu:photo:"))
async def lesson_photo(callback: CallbackQuery) -> None:
    await callback.answer()
    name = callback.data.removeprefix("edu:photo:")
    if not re.fullmatch(r"\d{2}-[a-z-]+\.png", name):
        return
    path = ROOT / "assets" / name
    if path.is_file() and isinstance(callback.message, Message):
        captions = json.loads((ROOT / "assets" / "manifest.json").read_text(encoding="utf-8"))
        await callback.message.answer_photo(
            FSInputFile(path),
            caption=captions.get(name[:-4], {}).get("caption", "")
            + "\nДемо-интерфейс Upscale, 25.10.2026. Учебные значения.",
        )
