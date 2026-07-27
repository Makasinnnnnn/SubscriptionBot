import re
from dataclasses import asdict, dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.settings_service import RuntimeSettingsService
from app.core.config import Settings, get_settings

USERNAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
MAX_SUPPORT_CONTACTS = 8


@dataclass(frozen=True)
class SupportContact:
    kind: str
    value: str
    label: str
    position: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedSupportContact:
    label: str
    url: str | None
    source: SupportContact


def normalize_support_contact(
    *,
    kind: str,
    value: str | int,
    label: str = "",
    position: int = 0,
) -> SupportContact:
    normalized_kind = kind.strip().lower()
    raw_value = str(value).strip()
    normalized_label = label.strip()[:48]
    if normalized_kind == "telegram_id":
        try:
            telegram_id = int(raw_value)
        except ValueError as exc:
            raise ValueError("Telegram ID должен быть целым положительным числом.") from exc
        if telegram_id <= 0:
            raise ValueError("Telegram ID должен быть целым положительным числом.")
        return SupportContact(
            kind="telegram_id",
            value=str(telegram_id),
            label=normalized_label or "Поддержка",
            position=max(0, int(position)),
        )
    if normalized_kind == "username":
        username = raw_value.removeprefix("@")
        if not USERNAME_PATTERN.fullmatch(username):
            raise ValueError(
                "Username должен содержать 5-32 латинских символа, цифры или '_'."
            )
        return SupportContact(
            kind="username",
            value=username,
            label=normalized_label or f"@{username}",
            position=max(0, int(position)),
        )
    raise ValueError("Тип контакта должен быть telegram_id или username.")


def support_contacts_from_settings(settings: Settings) -> list[SupportContact]:
    contacts: list[SupportContact] = []
    for telegram_id in settings.support_ids:
        contacts.append(
            normalize_support_contact(
                kind="telegram_id",
                value=telegram_id,
                position=len(contacts),
            )
        )
    raw_contacts = settings.support_contacts or [settings.support_contact]
    for raw in raw_contacts:
        for value in raw.replace(";", ",").split(","):
            candidate = value.strip()
            if not candidate:
                continue
            kind = "telegram_id" if candidate.lstrip("+").isdigit() else "username"
            try:
                contacts.append(
                    normalize_support_contact(
                        kind=kind,
                        value=candidate,
                        position=len(contacts),
                    )
                )
            except ValueError:
                continue
    return _deduplicate(contacts)[:MAX_SUPPORT_CONTACTS]


class SupportContactService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def definitions(self) -> list[SupportContact]:
        sentinel = object()
        raw = await RuntimeSettingsService(self.session).get(
            RuntimeSettingsService.SUPPORT_CONTACTS,
            sentinel,
        )
        if raw is sentinel:
            return support_contacts_from_settings(get_settings())
        contacts: list[SupportContact] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                contacts.append(
                    normalize_support_contact(
                        kind=str(item.get("kind", "")),
                        value=str(item.get("value", "")),
                        label=str(item.get("label", "")),
                        position=int(item.get("position", len(contacts))),
                    )
                )
            except (TypeError, ValueError):
                continue
        normalized = sorted(
            _deduplicate(contacts),
            key=lambda contact: contact.position,
        )[:MAX_SUPPORT_CONTACTS]
        return normalized or support_contacts_from_settings(get_settings())

    async def save(
        self,
        contacts: list[SupportContact],
        actor_telegram_id: int,
    ) -> None:
        normalized = _deduplicate(contacts)
        if not normalized:
            raise ValueError("Должен остаться хотя бы один контакт поддержки.")
        if len(normalized) > MAX_SUPPORT_CONTACTS:
            raise ValueError(f"Можно настроить не более {MAX_SUPPORT_CONTACTS} контактов.")
        values = [
            SupportContact(
                kind=contact.kind,
                value=contact.value,
                label=contact.label,
                position=index,
            ).to_json()
            for index, contact in enumerate(normalized)
        ]
        await RuntimeSettingsService(self.session).set(
            RuntimeSettingsService.SUPPORT_CONTACTS,
            values,
            actor_telegram_id,
        )

    async def reset_to_environment(self) -> bool:
        return await RuntimeSettingsService(self.session).delete(
            RuntimeSettingsService.SUPPORT_CONTACTS
        )

    async def resolved(self, bot: Bot) -> list[ResolvedSupportContact]:
        resolved: list[ResolvedSupportContact] = []
        for contact in await self.definitions():
            if contact.kind == "username":
                resolved.append(
                    ResolvedSupportContact(
                        label=contact.label or f"@{contact.value}",
                        url=f"https://t.me/{contact.value}",
                        source=contact,
                    )
                )
                continue
            username: str | None = None
            try:
                chat = await bot.get_chat(int(contact.value))
                username = chat.username
            except TelegramAPIError:
                pass
            if username:
                url = f"https://t.me/{username}"
                default_label = f"@{username}"
            else:
                url = None
                default_label = "Поддержка"
            resolved.append(
                ResolvedSupportContact(
                    label=contact.label or default_label,
                    url=url,
                    source=contact,
                )
            )
        unique: list[ResolvedSupportContact] = []
        positions: dict[str, int] = {}
        for contact in resolved:
            # A Telegram ID may resolve to the same public username that is also
            # configured explicitly. Display that profile only once and retain
            # the ID-backed definition because it can receive relayed tickets.
            key = (
                contact.url.lower().rstrip("/")
                if contact.url
                else f"{contact.source.kind}:{contact.source.value.lower()}"
            )
            existing_index = positions.get(key)
            if existing_index is not None:
                existing = unique[existing_index]
                if (
                    existing.source.kind != "telegram_id"
                    and contact.source.kind == "telegram_id"
                ):
                    unique[existing_index] = contact
                continue
            positions[key] = len(unique)
            unique.append(contact)
        return unique

    async def relay_target(self) -> SupportContact | None:
        """Return the configured Telegram account that receives support tickets."""
        return next(
            (
                contact
                for contact in await self.definitions()
                if contact.kind == "telegram_id"
            ),
            None,
        )


def _deduplicate(contacts: list[SupportContact]) -> list[SupportContact]:
    result: list[SupportContact] = []
    seen: set[tuple[str, str]] = set()
    for contact in contacts:
        key = (contact.kind, contact.value.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(contact)
    return result
