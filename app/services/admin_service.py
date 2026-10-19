"""Administrator management service."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminTransfer, Administrator
from app.repositories import AdministratorRepository, DialogRepository


class AdminService:
    """Operations related to human administrators."""

    def __init__(self, session: AsyncSession) -> None:
        self._admins = AdministratorRepository(session)
        self._dialogs = DialogRepository(session)

    async def is_admin(self, vk_id: int) -> bool:
        """Check if VK user is an active administrator."""
        return await self._admins.is_admin(vk_id)

    async def list_admins(self) -> list[Administrator]:
        """Return all active administrators."""
        return await self._admins.list_active()

    async def list_admins_except(self, vk_id: int) -> list[Administrator]:
        """Active administrators other than `vk_id` (a client who is also an admin)."""
        return [admin for admin in await self._admins.list_active() if admin.vk_id != vk_id]

    async def add_admin(self, vk_id: int, name: str | None = None) -> Administrator:
        """Register or re-activate an administrator."""
        existing = await self._admins.get_by_vk_id(vk_id)
        if existing:
            existing.is_active = True
            if name:
                existing.name = name
            return existing
        return await self._admins.create(vk_id, name)

    async def sync_admins(self, vk_ids: list[int]) -> None:
        """Make sure every configured admin exists and is active."""
        for vk_id in vk_ids:
            await self.add_admin(vk_id)

    async def get_active_transfer(self, user_vk_id: int) -> AdminTransfer | None:
        return await self._dialogs.get_active_transfer(user_vk_id)

    async def is_admin_engaged(self, user_vk_id: int) -> bool:
        """Check if a human admin has taken over the dialog."""
        return await self._dialogs.is_admin_engaged(user_vk_id)

    async def engage_admin(self, user_vk_id: int, admin_vk_id: int | None = None) -> str:
        """Mark that admin has taken the dialog. See DialogRepository.engage_admin."""
        return await self._dialogs.engage_admin(user_vk_id, admin_vk_id)

    async def resolve_transfer(self, user_vk_id: int) -> AdminTransfer | None:
        """Close the transfer and return the user to the bot."""
        return await self._dialogs.resolve_transfer(user_vk_id)

    async def create_transfer(self, user_vk_id: int, reason: str | None = None) -> None:
        """Register a transfer request."""
        await self._dialogs.create_transfer(user_vk_id, reason)
