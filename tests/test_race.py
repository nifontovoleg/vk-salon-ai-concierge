"""Gap 1: race-safe booking (real threads on a shared SQLite file)."""

import asyncio
import json
import threading

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base, Booking, BookingStatus, ServiceCategory
from app.repositories import BookingRepository, MasterRepository, ServiceRepository
from app.services.tools import ToolExecutor
from tests.helpers import future_day, make_settings

DAY = future_day(3)


async def _prepare(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await ServiceRepository(session).create("Женская стрижка", 2500, 60, ServiceCategory.HAIRCUT)
        await MasterRepository(session).create("Анна")
        await MasterRepository(session).create("Дмитрий")
        await session.commit()
    await engine.dispose()


def _run_threads(path, settings, calls):
    """Run create_booking calls in parallel threads, each with its own loop and engine."""
    barrier = threading.Barrier(len(calls))
    results: list[dict] = []
    lock = threading.Lock()

    def worker(user_id, args):
        async def run():
            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                executor = ToolExecutor(session, user_id, settings=settings)
                await session.execute(text("SELECT 1"))  # соединение уже открыто
                barrier.wait()
                result = json.loads(await executor.execute("create_booking", args))
                await session.commit()
            await engine.dispose()
            with lock:
                results.append(result)

        asyncio.run(run())

    threads = [threading.Thread(target=worker, args=(100 + i, a)) for i, a in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


async def _active(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.connect() as conn:
        rows = (await conn.execute(
            select(Booking.master_id, Booking.booking_time).where(Booking.status == BookingStatus.PENDING)
        )).all()
    await engine.dispose()
    return rows


def _args(time_, master=None):
    args = {"service_name": "Женская стрижка", "date": DAY.isoformat(), "time": time_, "phone": "+79991234567"}
    if master:
        args["master_name"] = master
    return args


@pytest.mark.parametrize("attempt", range(3))
async def test_same_slot_same_master_only_one_wins(tmp_path, attempt):
    path = tmp_path / "race.db"
    await _prepare(path)
    results = await asyncio.to_thread(_run_threads, path, make_settings(), [_args("12:00", "Анна")] * 6)
    winners = [r for r in results if r.get("success")]
    losers = [r for r in results if not r.get("success")]
    assert len(results) == 6 and len(winners) == 1
    for loser in losers:
        assert "занято" in loser["error"]
        assert loser["nearest_free_slots"] and all("12:00" not in s for s in loser["nearest_free_slots"][:1])
    assert len(await _active(path)) == 1


async def test_overlapping_different_starts(tmp_path):
    path = tmp_path / "race.db"
    await _prepare(path)
    settings = make_settings(slot_step_minutes=30)
    calls = [_args("12:00", "Анна"), _args("12:30", "Анна")] * 3
    results = await asyncio.to_thread(_run_threads, path, settings, calls)
    assert sum(1 for r in results if r.get("success")) == 1
    assert len(await _active(path)) == 1


async def test_auto_assign_under_race(tmp_path):
    path = tmp_path / "race.db"
    await _prepare(path)
    results = await asyncio.to_thread(_run_threads, path, make_settings(), [_args("15:00")] * 5)
    winners = [r for r in results if r.get("success")]
    assert len(winners) == 2 and {w["master"] for w in winners} == {"Анна", "Дмитрий"}
    assert len(await _active(path)) == 2


async def test_unique_index_blocks_duplicate_start(full_session):
    masters = await MasterRepository(full_session).list_active()
    services = await ServiceRepository(full_session).list_active()
    repo = BookingRepository(full_session)
    kwargs = dict(user_vk_id=1, service_id=services[0].id, master_id=masters[0].id,
                  booking_date=DAY, booking_time=__import__("datetime").time(12), client_phone="x")
    await repo.create(**kwargs)
    await full_session.commit()
    with pytest.raises(IntegrityError):
        await repo.create(**kwargs)
    await full_session.rollback()


async def test_old_db_gets_index_and_columns(tmp_path, monkeypatch):
    import sqlite3

    from app.config import get_settings
    from app.db import session as session_module

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE masters (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, specialization VARCHAR(255),
            is_active BOOLEAN NOT NULL, created_at DATETIME);
        CREATE TABLE bookings (id INTEGER PRIMARY KEY, user_vk_id BIGINT NOT NULL, service_id INTEGER NOT NULL,
            master_id INTEGER, booking_date DATE NOT NULL, booking_time TIME NOT NULL, client_phone VARCHAR(32) NOT NULL,
            client_name VARCHAR(255), status VARCHAR(20) NOT NULL, notes TEXT, created_at DATETIME);
        """
    )
    con.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    get_settings.cache_clear()
    old = session_module._engine, session_module._session_factory
    try:
        await session_module.init_db()
        await session_module.close_db()
    finally:
        session_module._engine, session_module._session_factory = old
        get_settings.cache_clear()
    con = sqlite3.connect(path)
    columns = {row[1] for row in con.execute("PRAGMA table_info(masters)")}
    indexes = {row[1] for row in con.execute("PRAGMA index_list(bookings)")}
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert {"working_hours", "days_off"} <= columns
    assert "uq_bookings_active_master_start" in indexes
    assert "booking_day_locks" in tables
