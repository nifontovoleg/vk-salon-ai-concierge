"""Async SQLAlchemy session management."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sqlalchemy import inspect, text

from app.config import get_settings
from app.db.models import ACTIVE_BOOKING_INDEX, ACTIVE_STATUS_SQL, Base

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _migrate_schema(connection) -> None:
    inspector = inspect(connection)
    if inspector.has_table("masters"):
        master_columns = {column["name"] for column in inspector.get_columns("masters")}
        if "working_hours" not in master_columns:
            connection.execute(text("ALTER TABLE masters ADD COLUMN working_hours VARCHAR(255)"))
        if "days_off" not in master_columns:
            connection.execute(text("ALTER TABLE masters ADD COLUMN days_off TEXT"))
    if not inspector.has_table("admin_transfers"):
        return
    columns = {column["name"] for column in inspector.get_columns("admin_transfers")}
    if "admin_engaged" not in columns:
        connection.execute(
            text("ALTER TABLE admin_transfers ADD COLUMN admin_engaged BOOLEAN NOT NULL DEFAULT FALSE")
        )
    if "admin_vk_id" not in columns:
        connection.execute(text("ALTER TABLE admin_transfers ADD COLUMN admin_vk_id BIGINT"))


def _ensure_sqlite_dir(database_url: str) -> None:
    if database_url.startswith("sqlite"):
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)


async def init_db() -> None:
    """Create engine, session factory and database tables."""
    global _engine, _session_factory

    settings = get_settings()
    _ensure_sqlite_dir(settings.database_url)

    connect_args: dict = {}
    if settings.database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    _engine = create_async_engine(
        settings.database_url,
        echo=False,
        connect_args=connect_args,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_schema)

    await _ensure_active_booking_index(_engine)
    logger.info("Database initialized: %s", settings.database_url.split("@")[-1])


async def _ensure_active_booking_index(engine: AsyncEngine) -> None:
    """Create the partial unique index on old databases (separate transaction)."""
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {ACTIVE_BOOKING_INDEX} "
                    f"ON bookings (master_id, booking_date, booking_time) WHERE {ACTIVE_STATUS_SQL}"
                )
            )
    except Exception as exc:
        logger.warning(
            "Не удалось создать уникальный индекс активных записей (есть дубли?): %s. "
            "Защита от гонок работает через блокировку дня.",
            exc,
        )


async def close_db() -> None:
    """Dispose database engine on shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("Database connection closed")


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide an async database session."""
    if _session_factory is None:
        raise RuntimeError("Database is not initialized. Call init_db() first.")

    session = _session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
