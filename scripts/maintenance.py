"""Fix admin list and clear stuck transfers."""

from __future__ import annotations

import asyncio

from sqlalchemy import select, update

from app.config import get_settings
from app.db.models import AdminTransfer, Administrator
from app.db.session import close_db, get_session, init_db


async def maintenance() -> None:
    settings = get_settings()
    await init_db()

    async with get_session() as session:
        await session.execute(
            update(Administrator)
            .where(Administrator.vk_id == 123456789)
            .values(is_active=False)
        )

        for vk_id in settings.seed_admin_ids:
            existing = await session.execute(
                select(Administrator).where(Administrator.vk_id == vk_id)
            )
            admin = existing.scalar_one_or_none()
            if admin is None:
                session.add(Administrator(vk_id=vk_id, name=f"Admin {vk_id}", is_active=True))
            else:
                admin.is_active = True

        await session.execute(
            update(AdminTransfer)
            .where(AdminTransfer.is_active.is_(True))
            .values(is_active=False, admin_engaged=False)
        )

    await close_db()


if __name__ == "__main__":
    asyncio.run(maintenance())
