from aiogram import F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from app.admin.service import AdminPermissionService
from app.admin.settings_service import RuntimeSettingsService
from app.bot.keyboards.admin import admin_cancel_keyboard, admin_menu_keyboard
from app.db.models import Channel, Plan
from app.db.session import async_session_factory

router = Router()


class ChannelForm(StatesGroup):
    chat_id = State()
    title = State()
    description = State()


class ChannelEditForm(StatesGroup):
    value = State()


@router.callback_query(F.data == "admin:channels")
async def channels_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not await _can_manage(callback):
        return
    await callback.answer()
    await _show_channels(callback)


@router.callback_query(F.data == "chan:add")
async def channel_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _can_manage(callback):
        return
    await state.clear()
    await state.update_data(actor_id=callback.from_user.id)
    await state.set_state(ChannelForm.chat_id)
    await callback.answer()
    await _edit(callback, "Добавление канала\n\nОтправьте chat_id канала, например -1001234567890.", admin_cancel_keyboard("admin:channels"))


@router.message(ChannelForm.chat_id)
async def channel_chat_id_received(message: Message, state: FSMContext) -> None:
    if not await _owns_state(message, state):
        return
    try:
        chat_id = int((message.text or "").strip())
        if chat_id >= 0:
            raise ValueError
    except ValueError:
        await message.answer("Нужен отрицательный Telegram chat_id, например -1001234567890.")
        return
    async with async_session_factory() as session:
        exists = await session.scalar(select(Channel.id).where(Channel.telegram_chat_id == chat_id))
    if exists is not None:
        await message.answer("Этот канал уже добавлен. Откройте его карточку в списке каналов.")
        return
    await state.update_data(chat_id=chat_id)
    await state.set_state(ChannelForm.title)
    await message.answer("Введите название канала (от 2 до 255 символов).", reply_markup=admin_cancel_keyboard("admin:channels"))


@router.message(ChannelForm.title)
async def channel_title_received(message: Message, state: FSMContext) -> None:
    if not await _owns_state(message, state):
        return
    title = (message.text or "").strip()
    if not 2 <= len(title) <= 255:
        await message.answer("Название должно содержать от 2 до 255 символов.")
        return
    await state.update_data(title=title)
    await state.set_state(ChannelForm.description)
    await message.answer("Введите описание или отправьте тире, если описание не нужно.", reply_markup=admin_cancel_keyboard("admin:channels"))


@router.message(ChannelForm.description)
async def channel_description_received(message: Message, state: FSMContext) -> None:
    if not await _owns_state(message, state):
        return
    data = await state.get_data()
    description = (message.text or "").strip()
    if description == "-":
        description = ""
    if len(description) > 2000:
        await message.answer("Описание не должно быть длиннее 2000 символов.")
        return
    async with async_session_factory() as session:
        permissions = AdminPermissionService(session)
        if not await permissions.can_manage_all_channels(message.from_user.id):
            await state.clear()
            await message.answer("Создавать новые каналы может только администратор с доступом ко всем каналам.", reply_markup=admin_menu_keyboard())
            return
        channel = Channel(telegram_chat_id=data["chat_id"], title=data["title"], description=description or None)
        try:
            await _verify_channel(message, channel.telegram_chat_id)
        except ChannelVerificationError as exc:
            await message.answer(str(exc), reply_markup=admin_cancel_keyboard("admin:channels"))
            return
        session.add(channel)
        await session.flush()
        await permissions.audit(message.from_user.id, "channel.create", "channel", channel.id)
        await session.commit()
    await state.clear()
    await message.answer("Канал добавлен и проверен. Теперь можно создать для него тариф.", reply_markup=admin_menu_keyboard())


@router.callback_query(F.data.startswith("chan:view:"))
async def channel_card(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if not await _can_manage(callback):
        return
    channel_id = _part_int(callback.data, 2)
    if channel_id is None:
        return
    await callback.answer()
    await _show_channel(callback, channel_id)


@router.callback_query(F.data.startswith("chan:edit:"))
async def channel_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _can_manage(callback):
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 4 or parts[3] not in {"title", "description"}:
        await callback.answer("Некорректное действие.", show_alert=True)
        return
    channel_id = _part_int(callback.data, 2)
    if channel_id is None or not await _channel_allowed(callback.from_user.id, channel_id):
        await callback.answer("Канал недоступен.", show_alert=True)
        return
    await state.update_data(actor_id=callback.from_user.id, channel_id=channel_id, field=parts[3])
    await state.set_state(ChannelEditForm.value)
    await callback.answer()
    prompt = "новое название (от 2 до 255 символов)" if parts[3] == "title" else "новое описание (до 2000 символов)"
    await _edit(callback, f"Введите {prompt}.", admin_cancel_keyboard(f"chan:view:{channel_id}"))


@router.message(ChannelEditForm.value)
async def channel_edit_value(message: Message, state: FSMContext) -> None:
    if not await _owns_state(message, state):
        return
    data = await state.get_data()
    value = (message.text or "").strip()
    if data["field"] == "title" and not 2 <= len(value) <= 255:
        await message.answer("Название должно содержать от 2 до 255 символов.")
        return
    if data["field"] == "description" and len(value) > 2000:
        await message.answer("Описание не должно быть длиннее 2000 символов.")
        return
    async with async_session_factory() as session:
        channel = await session.get(Channel, data["channel_id"])
        if channel is None or not await AdminPermissionService(session).can_manage_channel(message.from_user.id, channel.id):
            await state.clear()
            await message.answer("Канал недоступен.", reply_markup=admin_menu_keyboard())
            return
        setattr(channel, data["field"], value or None)
        await AdminPermissionService(session).audit(message.from_user.id, "channel.update", "channel", channel.id, {"field": data["field"]})
        await session.commit()
    await state.clear()
    await message.answer("Изменения сохранены.", reply_markup=admin_menu_keyboard())


@router.callback_query(F.data.startswith("chan:toggle:"))
async def channel_toggle(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    channel_id = _part_int(callback.data, 2)
    if channel_id is None:
        return
    async with async_session_factory() as session:
        channel = await session.get(Channel, channel_id)
        if channel is None or not await AdminPermissionService(session).can_manage_channel(callback.from_user.id, channel.id):
            await callback.answer("Канал недоступен.", show_alert=True)
            return
        channel.is_active = not channel.is_active
        await AdminPermissionService(session).audit(callback.from_user.id, "channel.enable" if channel.is_active else "channel.archive", "channel", channel.id)
        await session.commit()
    await callback.answer("Статус канала изменён.")
    await _show_channel(callback, channel_id)


@router.callback_query(F.data.startswith("chan:check:"))
async def channel_check(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    channel_id = _part_int(callback.data, 2)
    if channel_id is None:
        return
    async with async_session_factory() as session:
        channel = await session.get(Channel, channel_id)
    if channel is None:
        await callback.answer("Канал не найден.", show_alert=True)
        return
    try:
        await _verify_channel(callback, channel.telegram_chat_id)
    except ChannelVerificationError as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    await callback.answer("Бот администратор и имеет права на приглашения и удаление.", show_alert=True)


@router.callback_query(F.data == "chan:materials")
async def materials_menu(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    async with async_session_factory() as session:
        channels = await _managed_channels(session, callback.from_user.id)
        current = await RuntimeSettingsService(session).get(RuntimeSettingsService.MATERIALS_CHANNEL_ID, None)
    rows = [[InlineKeyboardButton(text="Отключить методички", callback_data="chan:materialset:0")]]
    rows.extend([[InlineKeyboardButton(text=f"{channel.title[:48]}{' ✓' if channel.telegram_chat_id == current else ''}", callback_data=f"chan:materialset:{channel.id}")] for channel in channels])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="admin:channels")])
    await callback.answer()
    await _edit(callback, "Канал методичек\n\nВыберите отдельный канал. Канал с тарифами нельзя выбрать одновременно.", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("chan:materialset:"))
async def materials_set(callback: CallbackQuery) -> None:
    if not await _can_manage(callback):
        return
    selected_id = _part_int(callback.data, 2)
    async with async_session_factory() as session:
        channel = None if selected_id in {None, 0} else await session.get(Channel, selected_id)
        if channel is not None:
            has_plan = await session.scalar(select(Plan.id).where(Plan.channel_id == channel.id, Plan.is_active.is_(True)))
            if has_plan is not None:
                await callback.answer("Канал с активным тарифом нельзя назначить каналом методичек.", show_alert=True)
                return
            if not await AdminPermissionService(session).can_manage_channel(callback.from_user.id, channel.id):
                await callback.answer("Канал недоступен.", show_alert=True)
                return
            try:
                await _verify_channel(callback, channel.telegram_chat_id)
            except ChannelVerificationError as exc:
                await callback.answer(str(exc)[:190], show_alert=True)
                return
            value = channel.telegram_chat_id
        else:
            value = None
        await RuntimeSettingsService(session).set(RuntimeSettingsService.MATERIALS_CHANNEL_ID, value, callback.from_user.id)
        await AdminPermissionService(session).audit(callback.from_user.id, "materials_channel.update", "setting", None, {"configured": value is not None})
        await session.commit()
    await callback.answer("Канал методичек сохранён.")
    await _show_channels(callback)


class ChannelVerificationError(RuntimeError):
    pass


async def _verify_channel(source: Message | CallbackQuery, chat_id: int) -> None:
    try:
        chat = await source.bot.get_chat(chat_id)
        bot_user = await source.bot.get_me()
        member = await source.bot.get_chat_member(chat.id, bot_user.id)
    except TelegramAPIError as exc:
        raise ChannelVerificationError("Не удалось найти канал. Проверьте chat_id и права бота.") from exc
    if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        raise ChannelVerificationError("Бот должен быть администратором канала.")
    if member.status == ChatMemberStatus.ADMINISTRATOR and not getattr(member, "can_invite_users", False):
        raise ChannelVerificationError("Боту нужно право приглашать пользователей по ссылке.")
    if member.status == ChatMemberStatus.ADMINISTRATOR and not getattr(member, "can_restrict_members", False):
        raise ChannelVerificationError("Боту нужно право удалять пользователей из канала.")


async def _show_channels(callback: CallbackQuery) -> None:
    async with async_session_factory() as session:
        channels = await _managed_all_channels(session, callback.from_user.id)
        current = await RuntimeSettingsService(session).get(RuntimeSettingsService.MATERIALS_CHANNEL_ID, None)
    lines = ["Каналы PropGate", ""]
    if not channels:
        lines.append("Каналов пока нет. Добавьте первый канал кнопкой ниже.")
    rows = [[InlineKeyboardButton(text=f"{'Активен' if channel.is_active else 'Архив'} | {channel.title[:48]}", callback_data=f"chan:view:{channel.id}")] for channel in channels]
    rows.extend([
        [InlineKeyboardButton(text="Добавить канал", callback_data="chan:add")],
        [InlineKeyboardButton(text=f"Методички: {'настроены' if current else 'не настроены'}", callback_data="chan:materials")],
        [InlineKeyboardButton(text="В меню", callback_data="admin:menu")],
    ])
    await _edit(callback, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


async def _show_channel(callback: CallbackQuery, channel_id: int) -> None:
    async with async_session_factory() as session:
        channel = await session.get(Channel, channel_id)
        allowed = channel is not None and await AdminPermissionService(session).can_manage_channel(callback.from_user.id, channel_id)
    if not allowed or channel is None:
        await callback.answer("Канал недоступен.", show_alert=True)
        return
    status = "активен" if channel.is_active else "в архиве"
    text = f"{channel.title}\n\nID: {channel.telegram_chat_id}\nСтатус: {status}\nОписание: {channel.description or 'не задано'}"
    rows = [
        [InlineKeyboardButton(text="Изменить название", callback_data=f"chan:edit:{channel_id}:title")],
        [InlineKeyboardButton(text="Изменить описание", callback_data=f"chan:edit:{channel_id}:description")],
        [InlineKeyboardButton(text="Проверить права бота", callback_data=f"chan:check:{channel_id}")],
        [InlineKeyboardButton(text="Архивировать" if channel.is_active else "Вернуть в активные", callback_data=f"chan:toggle:{channel_id}")],
        [InlineKeyboardButton(text="К списку каналов", callback_data="admin:channels")],
    ]
    await _edit(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def _managed_all_channels(session, telegram_id: int) -> list[Channel]:
    channels = list(await session.scalars(select(Channel).order_by(Channel.is_active.desc(), Channel.title)))
    permissions = AdminPermissionService(session)
    return [channel for channel in channels if await permissions.can_manage_channel(telegram_id, channel.id)]


async def _managed_channels(session, telegram_id: int) -> list[Channel]:
    return [channel for channel in await _managed_all_channels(session, telegram_id) if channel.is_active]


async def _channel_allowed(telegram_id: int, channel_id: int) -> bool:
    async with async_session_factory() as session:
        return await AdminPermissionService(session).can_manage_channel(telegram_id, channel_id)


async def _can_manage(callback: CallbackQuery) -> bool:
    async with async_session_factory() as session:
        allowed = await AdminPermissionService(session).can_manage(callback.from_user.id)
    if not allowed:
        await callback.answer("Недостаточно прав.", show_alert=True)
    return allowed


async def _owns_state(message: Message, state: FSMContext) -> bool:
    data = await state.get_data()
    if data.get("actor_id") != message.from_user.id:
        return False
    async with async_session_factory() as session:
        if not await AdminPermissionService(session).can_manage(message.from_user.id):
            await state.clear()
            await message.answer("Недостаточно прав.")
            return False
    return True


async def _edit(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def _part_int(data: str | None, index: int) -> int | None:
    try:
        return int((data or "").split(":")[index])
    except (IndexError, ValueError):
        return None
