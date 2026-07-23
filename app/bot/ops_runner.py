import json
import logging
from datetime import datetime
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.enums import SubscriptionStatus
from app.db.models import AuditLog, InviteLink, Payment, Subscription, User, WebhookEvent
from app.db.session import async_session_factory

logger = logging.getLogger(__name__)
router = Router()
HEALTH_EVENTS_FILE = Path("/monitor/events.jsonl")


class OpsSearch(StatesGroup):
    query = State()


def _menu() -> InlineKeyboardMarkup:
    rows = [
        [_button("API", "ops:api"), _button("Telegram", "ops:telegram")],
        [_button("Webhook", "ops:webhooks"), _button("Сервисы", "ops:services")],
        [_button("Доступы", "ops:access"), _button("Платежи", "ops:payments")],
        [_button("База данных", "ops:db"), _button("Ссылки", "ops:links")],
        [_button("Подписки", "ops:subscriptions"), _button("Пользователи", "ops:users")],
        [_button("Падения", "ops:server_down"), _button("Восстановления", "ops:server_up")],
        [_button("Поиск", "ops:search"), _button("Обновить", "ops:menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back(section: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button("Обновить", f"ops:{section}"), _button("Меню", "ops:menu")]]
    )


def _button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


@router.message(Command("start", "ops"))
async def ops_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not _allowed(message.from_user.id if message.from_user else None, message.chat.id):
        return
    await message.answer(
        "PropGate Operations\n\nВыберите раздел. Push приходит только по важным событиям; "
        "остальную историю можно открыть кнопками.",
        reply_markup=_menu(),
    )


@router.callback_query(F.data == "ops:menu")
async def ops_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not await _allowed_callback(callback):
        return
    await callback.answer()
    await _edit(callback, "PropGate Operations\n\nВыберите раздел:", _menu())


@router.callback_query(F.data.startswith("ops:"))
async def ops_section(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _allowed_callback(callback):
        return
    section = (callback.data or "").partition(":")[2]
    if section == "search":
        await state.set_state(OpsSearch.query)
        await callback.answer()
        await _edit(
            callback,
            "Введите username или Telegram ID пользователя:",
            InlineKeyboardMarkup(inline_keyboard=[[_button("Отмена", "ops:menu")]]),
        )
        return
    await state.update_data(last_section=section)
    await callback.answer()
    await _show_section(callback, section)


@router.message(OpsSearch.query)
async def ops_search(message: Message, state: FSMContext) -> None:
    if not _allowed(message.from_user.id if message.from_user else None, message.chat.id):
        await state.clear()
        return
    value = (message.text or "").strip()
    if not value or len(value) > 255:
        await message.answer("Введите корректный username или Telegram ID.")
        return
    async with async_session_factory() as session:
        query = select(User)
        if value.removeprefix("-").isdigit():
            query = query.where(User.telegram_id == int(value))
        else:
            username = value.removeprefix("@").lower()
            query = query.where(func.lower(User.username) == username)
        user = await session.scalar(query)
        subscriptions = [] if user is None else list(
            await session.scalars(
                select(Subscription)
                .options(selectinload(Subscription.channel), selectinload(Subscription.user))
                .where(Subscription.user_id == user.id)
                .order_by(Subscription.created_at.desc())
                .limit(20)
            )
        )
        payments = [] if user is None else list(
            await session.scalars(
                select(Payment)
                .options(selectinload(Payment.user))
                .where(Payment.user_id == user.id)
                .order_by(Payment.created_at.desc())
                .limit(5)
            )
        )
    await state.clear()
    if user is None:
        await message.answer("Пользователь не найден.", reply_markup=_menu())
        return
    identity = f"@{user.username}" if user.username else "username не задан"
    lines = [f"Пользователь {identity}", f"Telegram ID: {user.telegram_id}", "", "Подписки:"]
    lines.extend(_subscription_lines(subscriptions) or ["нет"])
    lines.append("\nПоследние платежи:")
    lines.extend(_payment_lines(payments) or ["нет"])
    await message.answer("\n".join(lines)[:4000], reply_markup=_menu())


async def _show_section(callback: CallbackQuery, section: str) -> None:
    builders = {
        "api": _api_text,
        "telegram": lambda: _telegram_text(callback.bot),
        "webhooks": _webhook_text,
        "services": lambda: _services_text(callback.bot),
        "access": _access_text,
        "payments": _payments_text,
        "db": _db_text,
        "links": _links_text,
        "subscriptions": _subscriptions_text,
        "users": _users_text,
        "server_down": lambda: _health_events_text("down"),
        "server_up": lambda: _health_events_text("up"),
    }
    builder = builders.get(section)
    text = "Раздел не найден." if builder is None else await builder()
    await _edit(callback, text[:4000], _back(section))


async def _api_text() -> str:
    base_url = get_settings().ops_api_url.rstrip("/")
    results = []
    async with httpx.AsyncClient(timeout=5) as client:
        for path in ("/health", "/health/db"):
            try:
                response = await client.get(f"{base_url}{path}")
                results.append(f"{path}: HTTP {response.status_code}")
            except httpx.HTTPError as exc:
                results.append(f"{path}: недоступен ({type(exc).__name__})")
    return "Работа API\n\n" + "\n".join(results)


async def _telegram_text(bot: Bot) -> str:
    try:
        me = await bot.get_me()
        return f"Работа Telegram\n\nOps bot: @{me.username or 'без username'}\nAPI Telegram: доступен"
    except Exception as exc:
        return f"Работа Telegram\n\nAPI Telegram: ошибка {type(exc).__name__}"


async def _services_text(bot: Bot) -> str:
    api = await _api_text()
    telegram = await _telegram_text(bot)
    return f"Работа сервисов\n\n{api.splitlines()[-2]}\n{api.splitlines()[-1]}\n{telegram.splitlines()[-1]}"


async def _webhook_text() -> str:
    async with async_session_factory() as session:
        events = list(await session.scalars(select(WebhookEvent).order_by(WebhookEvent.created_at.desc()).limit(20)))
    lines = ["Webhook: последние события", ""]
    lines.extend(
        f"{_dt(item.created_at)} | {item.provider} | {item.event_type} | {item.error or 'ok'}"
        for item in events
    )
    return "\n".join(lines if events else [*lines, "Событий нет."])


async def _access_text() -> str:
    async with async_session_factory() as session:
        logs = list(
            await session.scalars(
                select(AuditLog)
                .where(or_(AuditLog.action.like("subscription.%"), AuditLog.action.like("invite.%")))
                .order_by(AuditLog.created_at.desc())
                .limit(25)
            )
        )
    lines = ["Доступы: последние действия", ""]
    lines.extend(f"{_dt(item.created_at)} | {item.action} | #{item.target_id or '-'}" for item in logs)
    return "\n".join(lines if logs else [*lines, "Действий нет."])


async def _payments_text() -> str:
    async with async_session_factory() as session:
        payments = list(
            await session.scalars(
                select(Payment)
                .options(selectinload(Payment.user))
                .order_by(Payment.created_at.desc())
                .limit(25)
            )
        )
    return "Последние платежи\n\n" + "\n".join(_payment_lines(payments) or ["Платежей нет."])


async def _db_text() -> str:
    async with async_session_factory() as session:
        counts = {
            "users": await session.scalar(select(func.count(User.id))) or 0,
            "payments": await session.scalar(select(func.count(Payment.id))) or 0,
            "subscriptions": await session.scalar(select(func.count(Subscription.id))) or 0,
            "webhooks": await session.scalar(select(func.count(WebhookEvent.id))) or 0,
        }
    return "База данных\n\nСоединение: доступно\n" + "\n".join(f"{key}: {value}" for key, value in counts.items())


async def _links_text() -> str:
    async with async_session_factory() as session:
        links = list(await session.scalars(select(InviteLink).order_by(InviteLink.updated_at.desc()).limit(25)))
    lines = ["Invite-ссылки", ""]
    lines.extend(
        f"{_dt(item.updated_at)} | sub #{item.subscription_id} | "
        f"main={'revoked' if item.is_revoked else 'active'} | "
        f"materials={'revoked' if item.materials_is_revoked else 'active' if item.materials_link else 'none'}"
        for item in links
    )
    return "\n".join(lines if links else [*lines, "Ссылок нет."])


async def _subscriptions_text() -> str:
    async with async_session_factory() as session:
        subscriptions = list(
            await session.scalars(
                select(Subscription)
                .options(selectinload(Subscription.user), selectinload(Subscription.channel))
                .order_by(Subscription.created_at.desc())
                .limit(25)
            )
        )
        active = await session.scalar(
            select(func.count(Subscription.id)).where(Subscription.status == SubscriptionStatus.ACTIVE)
        ) or 0
    return f"Подписки\n\nАктивных: {active}\n\n" + "\n".join(_subscription_lines(subscriptions) or ["Подписок нет."])


async def _users_text() -> str:
    async with async_session_factory() as session:
        users = list(
            await session.scalars(
                select(User)
                .join(Subscription)
                .where(Subscription.status == SubscriptionStatus.ACTIVE)
                .distinct()
                .order_by(User.updated_at.desc())
                .limit(30)
            )
        )
    lines = ["Пользователи с активной подпиской", ""]
    lines.extend(f"@{user.username} | {user.telegram_id}" if user.username else str(user.telegram_id) for user in users)
    return "\n".join(lines if users else [*lines, "Пользователей нет."])


async def _health_events_text(status: str) -> str:
    title = "Падения сервера" if status == "down" else "Восстановления сервера"
    events = []
    try:
        for line in HEALTH_EVENTS_FILE.read_text(encoding="utf-8").splitlines()[-200:]:
            event = json.loads(line)
            if event.get("status") == status:
                events.append(event)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    lines = [title, ""]
    lines.extend(f"{item.get('time', '-')} | {str(item.get('details', ''))[:120]}" for item in events[-20:][::-1])
    return "\n".join(lines if events else [*lines, "Событий пока нет."])


def _subscription_lines(items: list[Subscription]) -> list[str]:
    return [
        f"#{item.id} | @{item.user.username or item.user.telegram_id} | {item.channel.title} | "
        f"{item.status.value} | до {_dt(item.expires_at)}"
        for item in items
    ]


def _payment_lines(items: list[Payment]) -> list[str]:
    return [
        f"#{item.id} | @{item.user.username or item.user.telegram_id} | {item.amount:g} {item.currency} | "
        f"{item.provider} | {item.status.value}"
        for item in items
    ]


def _dt(value: datetime | None) -> str:
    return value.strftime("%d.%m %H:%M") if value else "-"


def _allowed(user_id: int | None, chat_id: int) -> bool:
    settings = get_settings()
    allowed = set(settings.ops_chat_ids) | set(settings.admin_ids)
    return user_id in allowed or chat_id in allowed


async def _allowed_callback(callback: CallbackQuery) -> bool:
    chat_id = callback.message.chat.id if callback.message else 0
    if _allowed(callback.from_user.id, chat_id):
        return True
    await callback.answer("Нет доступа.", show_alert=True)
    return False


async def _edit(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def run_ops_bot() -> None:
    setup_logging()
    settings = get_settings()
    if not settings.ops_bot_token or not settings.ops_chat_ids:
        raise RuntimeError("OPS_BOT_TOKEN and OPS_CHAT_IDS are required for APP_MODE=ops")
    bot = Bot(settings.ops_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
