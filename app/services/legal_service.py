from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.service import AdminPermissionService
from app.core.config import Settings, get_settings
from app.db.models import LegalAcceptance, User


class LegalAcceptanceRequiredError(ValueError):
    pass


class LegalService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    async def is_accepted(self, user_id: int) -> bool:
        acceptance_id = await self.session.scalar(
            select(LegalAcceptance.id).where(
                LegalAcceptance.user_id == user_id,
                LegalAcceptance.legal_version == self.settings.legal_version,
                LegalAcceptance.terms_version == self.settings.legal_version,
                LegalAcceptance.privacy_version == self.settings.legal_version,
                LegalAcceptance.refund_policy_version == self.settings.legal_version,
            )
        )
        return acceptance_id is not None

    async def require(self, user_id: int) -> None:
        if not await self.is_accepted(user_id):
            raise LegalAcceptanceRequiredError("Current legal documents must be accepted")

    async def accept(self, user: User) -> LegalAcceptance:
        existing = await self.session.scalar(
            select(LegalAcceptance).where(
                LegalAcceptance.user_id == user.id,
                LegalAcceptance.legal_version == self.settings.legal_version,
            )
        )
        if existing is not None:
            return existing
        acceptance = LegalAcceptance(
            user_id=user.id,
            telegram_id=user.telegram_id,
            legal_version=self.settings.legal_version,
            terms_version=self.settings.legal_version,
            privacy_version=self.settings.legal_version,
            refund_policy_version=self.settings.legal_version,
            accepted_at=datetime.now(UTC).replace(tzinfo=None),
        )
        self.session.add(acceptance)
        await self.session.flush()
        await AdminPermissionService(self.session).audit(
            user.telegram_id,
            "legal.accept",
            "user",
            user.id,
            {"legal_version": self.settings.legal_version},
        )
        return acceptance
