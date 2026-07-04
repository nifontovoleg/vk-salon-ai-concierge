"""Booking business logic."""

from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Booking
from app.repositories import BookingRepository, MasterRepository, ServiceRepository


class BookingService:
    """High-level booking operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._bookings = BookingRepository(session)
        self._services = ServiceRepository(session)
        self._masters = MasterRepository(session)

    async def get_user_bookings(self, user_vk_id: int) -> list[Booking]:
        """Return all bookings for a VK user."""
        return await self._bookings.list_by_user(user_vk_id)

    async def get_available_slots(self, booking_date: date, master_id: int | None = None) -> list[str]:
        """Return available time slots as HH:MM strings."""
        from app.services.tools import _generate_slots

        booked = await self._bookings.get_booked_times(booking_date, master_id)
        booked_set = set(booked)
        return [t.strftime("%H:%M") for t in _generate_slots() if t not in booked_set]
