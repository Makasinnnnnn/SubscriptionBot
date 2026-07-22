from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Admin, AdminChannelAccess, AuditLog


class AdminPermissionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def is_superadmin(self, telegram_id: int) -> bool:
        return telegram_id in get_settings().admin_ids

    async def get_db_admin(self, telegram_id: int) -> Admin | None:
        return await self.session.scalar(
            select(Admin).where(
                Admin.telegram_id == telegram_id,
                Admin.is_active.is_(True),
            )
        )

    async def is_admin(self, telegram_id: int) -> bool:
        return self.is_superadmin(telegram_id) or await self.get_db_admin(telegram_id) is not None

    async def can_manage(self, telegram_id: int) -> bool:
        if self.is_superadmin(telegram_id):
            return True
        admin = await self.get_db_admin(telegram_id)
        return admin is not None and admin.role in {"admin", "superadmin"}

    async def can_manage_channel(self, telegram_id: int, channel_id: int) -> bool:
        if self.is_superadmin(telegram_id):
            return True
        admin = await self.get_db_admin(telegram_id)
        if admin is None or admin.role not in {"admin", "superadmin"}:
            return False
        if admin.all_channels:
            return True
        access_id = await self.session.scalar(
            select(AdminChannelAccess.id).where(
                AdminChannelAccess.admin_id == admin.id,
                AdminChannelAccess.channel_id == channel_id,
                AdminChannelAccess.can_manage.is_(True),
            )
        )
        return access_id is not None

    async def can_manage_all_channels(self, telegram_id: int) -> bool:
        if self.is_superadmin(telegram_id):
            return True
        admin = await self.get_db_admin(telegram_id)
        return (
            admin is not None
            and admin.role in {"admin", "superadmin"}
            and admin.all_channels
        )

    async def channel_scope(
        self,
        telegram_id: int,
        *,
        require_manage: bool = False,
    ) -> list[int] | None:
        """Return None for global access, or the exact allowed channel IDs."""
        if self.is_superadmin(telegram_id):
            return None
        admin = await self.get_db_admin(telegram_id)
        if admin is None:
            return []
        if require_manage and admin.role not in {"admin", "superadmin"}:
            return []
        if admin.all_channels:
            return None
        return list(
            await self.session.scalars(
                select(AdminChannelAccess.channel_id).where(
                    AdminChannelAccess.admin_id == admin.id,
                    AdminChannelAccess.can_manage.is_(True),
                )
            )
        )

    async def add_admin(self, telegram_id: int, role: str = "admin") -> Admin:
        if role not in {"admin", "support"}:
            raise ValueError("Role must be admin or support")
        admin = await self.session.scalar(select(Admin).where(Admin.telegram_id == telegram_id))
        if admin is None:
            admin = Admin(telegram_id=telegram_id, role=role)
            self.session.add(admin)
        else:
            admin.role = role
            admin.is_active = True
        await self.session.flush()
        return admin

    async def set_all_channels(self, telegram_id: int, all_channels: bool) -> Admin:
        admin = await self.session.scalar(select(Admin).where(Admin.telegram_id == telegram_id))
        if admin is None or not admin.is_active:
            raise ValueError("Admin not found")
        admin.all_channels = all_channels
        await self.session.flush()
        return admin

    async def grant_channel(self, telegram_id: int, channel_id: int) -> AdminChannelAccess:
        admin = await self.session.scalar(select(Admin).where(Admin.telegram_id == telegram_id))
        if admin is None or not admin.is_active:
            raise ValueError("Admin not found")
        access = await self.session.scalar(
            select(AdminChannelAccess).where(
                AdminChannelAccess.admin_id == admin.id,
                AdminChannelAccess.channel_id == channel_id,
            )
        )
        if access is None:
            access = AdminChannelAccess(admin_id=admin.id, channel_id=channel_id)
            self.session.add(access)
        access.can_manage = True
        await self.session.flush()
        return access

    async def revoke_channel(self, telegram_id: int, channel_id: int) -> bool:
        admin = await self.session.scalar(select(Admin).where(Admin.telegram_id == telegram_id))
        if admin is None:
            return False
        access = await self.session.scalar(
            select(AdminChannelAccess).where(
                AdminChannelAccess.admin_id == admin.id,
                AdminChannelAccess.channel_id == channel_id,
            )
        )
        if access is None:
            return False
        await self.session.delete(access)
        await self.session.flush()
        return True

    async def remove_admin(self, telegram_id: int) -> bool:
        admin = await self.session.scalar(select(Admin).where(Admin.telegram_id == telegram_id))
        if admin is None:
            return False
        admin.is_active = False
        await self.session.flush()
        return True

    async def audit(
        self,
        actor_telegram_id: int | None,
        action: str,
        target_type: str,
        target_id: int | str | None = None,
        metadata: dict | None = None,
    ) -> AuditLog:
        log = AuditLog(
            actor_telegram_id=actor_telegram_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            metadata_json=metadata or {},
        )
        self.session.add(log)
        await self.session.flush()
        return log
