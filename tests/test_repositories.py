"""Repository tests."""

from datetime import date, time

import pytest

from app.repositories import BookingRepository, MasterRepository, ServiceRepository


@pytest.mark.asyncio
async def test_service_repository_list_active(seeded_session):
    repo = ServiceRepository(seeded_session)
    services = await repo.list_active()
    assert len(services) == 2
    assert services[0].price > 0


@pytest.mark.asyncio
async def test_service_repository_get_by_name(seeded_session):
    repo = ServiceRepository(seeded_session)
    service = await repo.get_by_name("Женская стрижка")
    assert service is not None
    assert service.price == 2500


@pytest.mark.asyncio
async def test_master_repository_create(session):
    repo = MasterRepository(session)
    master = await repo.create("Анна", "Стрижки")
    await session.commit()
    assert master.id is not None
    assert master.name == "Анна"


@pytest.mark.asyncio
async def test_booking_repository_create(seeded_session):
    service_repo = ServiceRepository(seeded_session)
    services = await service_repo.list_active()
    booking_repo = BookingRepository(seeded_session)

    booking = await booking_repo.create(
        user_vk_id=1001,
        service_id=services[0].id,
        booking_date=date(2026, 7, 10),
        booking_time=time(12, 0),
        client_phone="+79991234567",
    )
    await seeded_session.commit()

    booked_times = await booking_repo.get_booked_times(date(2026, 7, 10))
    assert time(12, 0) in booked_times
    assert booking.status == "pending"
