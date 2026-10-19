"""Booking business logic: admin notifications and confirmation."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Booking, BookingStatus
from app.repositories import BookingRepository

STATUS_LABELS = {
    BookingStatus.PENDING: "ожидает подтверждения",
    BookingStatus.CONFIRMED: "подтверждена",
    BookingStatus.CANCELLED: "отменена",
    BookingStatus.COMPLETED: "завершена",
}


def _when(booking: Booking) -> str:
    return f"{booking.booking_date.strftime('%d.%m.%Y')} в {booking.booking_time.strftime('%H:%M')}"


def format_admin_notification(booking: Booking) -> str:
    service = booking.service
    master = booking.master.name if booking.master else "любой свободный"
    client = booking.client_name or "имя не указано"
    return (
        f"Новая запись #{booking.id} (ожидает подтверждения)\n"
        f"Услуга: {service.name} — {service.price} ₽, {service.duration_minutes} мин\n"
        f"Мастер: {master}\n"
        f"Когда: {_when(booking)}\n"
        f"Клиент: {client}, {booking.client_phone}, vk.com/id{booking.user_vk_id}"
    )


def format_client_status(booking: Booking, settings: Settings) -> str:
    master = f", мастер {booking.master.name}" if booking.master else ""
    details = f"{booking.service.name}, {_when(booking)}{master}"
    if booking.status == BookingStatus.CONFIRMED:
        address = f"\nАдрес: {settings.salon_address}" if settings.salon_address else ""
        return f"Ваша запись подтверждена: {details}.{address}\nЖдём вас!"
    phone = f" или позвоните {settings.salon_phone}" if settings.salon_phone else ""
    return (
        f"К сожалению, администратор отменил запись: {details}.\n"
        f"Напишите, и подберём другое время{phone}."
    )


class BookingService:
    """High-level booking operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._bookings = BookingRepository(session)

    async def get(self, booking_id: int) -> Booking | None:
        return await self._bookings.get_with_relations(booking_id)

    async def get_user_bookings(self, user_vk_id: int) -> list[Booking]:
        return await self._bookings.list_by_user(user_vk_id)

    async def set_status(self, booking_id: int, status: str) -> tuple[Booking | None, str]:
        """Move a pending booking to confirmed/cancelled.

        Returns (booking, outcome) where outcome is "ok", "not_found" or "already".
        """
        if status not in (BookingStatus.CONFIRMED, BookingStatus.CANCELLED):
            raise ValueError(f"Unsupported status: {status}")
        booking = await self._bookings.get_with_relations(booking_id)
        if booking is None:
            return None, "not_found"
        if booking.status != BookingStatus.PENDING:
            return booking, "already"
        booking.status = status
        return booking, "ok"
