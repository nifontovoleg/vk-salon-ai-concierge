"""Seed database with minimal sample salon data."""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Service, ServiceCategory
from app.db.session import close_db, get_session, init_db
from app.logging.setup import setup_logging
from app.repositories import AdministratorRepository, MasterRepository, ServiceRepository

logger = logging.getLogger(__name__)

# Minimal set for local development and demos
SEED_SERVICES = [
    ("Женская стрижка", "Стрижка с мытьём и укладкой", 2500, 60, ServiceCategory.HAIRCUT),
    ("Мужская стрижка", "Классическая стрижка", 1500, 45, ServiceCategory.HAIRCUT),
    ("Маникюр", "Классический маникюр", 1800, 60, ServiceCategory.MANICURE),
]

# (имя, специализация, часы работы — пусто = часы салона, выходные даты)
SEED_MASTERS = [
    ("Анна", "Стрижки, окрашивание", "Пн-Пт 10:00-18:00", None),
    ("Дмитрий", "Мужские стрижки", "Ср-Вс 12:00-20:00", None),
]


async def seed() -> None:
    """Populate database with initial data if empty."""
    settings = get_settings()
    setup_logging(settings.log_level)
    await init_db()

    try:
        async with get_session() as session:
            existing = await session.execute(select(Service).limit(1))
            if existing.scalar_one_or_none() is not None:
                logger.info("Database already seeded, skipping.")
                return

            service_repo = ServiceRepository(session)
            for name, desc, price, duration, category in SEED_SERVICES:
                await service_repo.create(name, price, duration, category, desc)
                logger.info("Added service: %s (%s rub)", name, price)

            master_repo = MasterRepository(session)
            for name, spec, hours, days_off in SEED_MASTERS:
                await master_repo.create(name, spec, working_hours=hours, days_off=days_off)
                logger.info("Added master: %s", name)

            admin_repo = AdministratorRepository(session)
            for vk_id in settings.seed_admin_ids:
                await admin_repo.create(vk_id, name=f"Admin {vk_id}")
                logger.info("Added administrator: vk_id=%s", vk_id)
    finally:
        await close_db()

    logger.info("Seed completed successfully.")


if __name__ == "__main__":
    asyncio.run(seed())
