"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base, ServiceCategory
from app.repositories import AdministratorRepository, ConsentRepository, MasterRepository, ServiceRepository

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

os.environ.setdefault("VK_GROUP_TOKEN", "test_vk_token")
os.environ.setdefault("VK_GROUP_ID", "123456789")
os.environ.setdefault("OPENROUTER_API_KEY", "test_openrouter_key")

from tests.helpers import ADMIN_ID, CLIENT_ID, FakeApi, make_settings  # noqa: E402

@pytest.fixture
def settings():
    return make_settings()


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess

    await engine.dispose()


async def _seed(session: AsyncSession) -> None:
    repo = ServiceRepository(session)
    await repo.create("Женская стрижка", 2500, 60, ServiceCategory.HAIRCUT, "Описание")
    await repo.create("Мужская стрижка", 1500, 45, ServiceCategory.HAIRCUT)


@pytest_asyncio.fixture
async def seeded_session(session: AsyncSession) -> AsyncSession:
    await _seed(session)
    await session.commit()
    return session


@pytest_asyncio.fixture
async def full_session(seeded_session: AsyncSession) -> AsyncSession:
    """Services + two masters + one admin + consent of CLIENT_ID."""
    masters = MasterRepository(seeded_session)
    await masters.create("Анна", "Стрижки")
    await masters.create("Дмитрий", "Мужские стрижки")
    await AdministratorRepository(seeded_session).create(ADMIN_ID, "Admin")
    await ConsentRepository(seeded_session).give(CLIENT_ID, "https://example.com/privacy")
    await seeded_session.commit()
    return seeded_session


@pytest_asyncio.fixture
async def app_db(tmp_path):
    """File-based DB wired into app.db.session for handler-level tests."""
    from app.db import session as session_module

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    old = session_module._engine, session_module._session_factory
    session_module._engine, session_module._session_factory = engine, factory

    async with factory() as sess:
        await _seed(sess)
        masters = MasterRepository(sess)
        await masters.create("Анна", "Стрижки")
        await masters.create("Дмитрий", "Мужские стрижки")
        await AdministratorRepository(sess).create(ADMIN_ID, "Admin")
        await ConsentRepository(sess).give(CLIENT_ID, None)
        await sess.commit()

    yield factory

    session_module._engine, session_module._session_factory = old
    await engine.dispose()


@pytest.fixture
def fake_api() -> FakeApi:
    return FakeApi()
