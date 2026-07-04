"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base
from app.db.models import Service, ServiceCategory
from app.repositories import ServiceRepository

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

os.environ.setdefault("VK_GROUP_TOKEN", "test_vk_token")
os.environ.setdefault("VK_GROUP_ID", "123456789")
os.environ.setdefault("OPENROUTER_API_KEY", "test_openrouter_key")


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess

    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_session(session: AsyncSession) -> AsyncSession:
    repo = ServiceRepository(session)
    await repo.create("Женская стрижка", 2500, 60, ServiceCategory.HAIRCUT, "Описание")
    await repo.create("Мужская стрижка", 1500, 45, ServiceCategory.HAIRCUT)
    await session.commit()
    return session
