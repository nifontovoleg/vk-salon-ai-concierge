"""Data access layer."""

from datetime import datetime, timezone

from sqlalchemy import and_, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    AdminTransfer,
    Administrator,
    Booking,
    BookingStatus,
    ClientConsent,
    DialogMessage,
    Master,
    ProcessedVkMessage,
    Service,
)


class ServiceRepository:
    """CRUD operations for salon services."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self) -> list[Service]:
        result = await self._session.execute(
            select(Service).where(Service.is_active.is_(True)).order_by(Service.category, Service.name)
        )
        return list(result.scalars().all())

    async def get_by_id(self, service_id: int) -> Service | None:
        result = await self._session.execute(select(Service).where(Service.id == service_id))
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Service | None:
        result = await self._session.execute(
            select(Service).where(Service.name == name, Service.is_active.is_(True))
        )
        service = result.scalar_one_or_none()
        if service is not None:
            return service
        for item in await self.list_active():
            if item.name.lower() == name.lower():
                return item
        return None

    async def create(
        self,
        name: str,
        price: int,
        duration_minutes: int,
        category: str,
        description: str | None = None,
    ) -> Service:
        service = Service(
            name=name,
            price=price,
            duration_minutes=duration_minutes,
            category=category,
            description=description,
        )
        self._session.add(service)
        await self._session.flush()
        return service


class MasterRepository:
    """CRUD operations for masters."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self) -> list[Master]:
        result = await self._session.execute(
            select(Master).where(Master.is_active.is_(True)).order_by(Master.name)
        )
        return list(result.scalars().all())

    async def get_by_id(self, master_id: int) -> Master | None:
        result = await self._session.execute(select(Master).where(Master.id == master_id))
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Master | None:
        result = await self._session.execute(
            select(Master).where(Master.name == name, Master.is_active.is_(True))
        )
        master = result.scalar_one_or_none()
        if master is not None:
            return master
        for item in await self.list_active():
            if item.name.lower() == name.lower():
                return item
        return None

    async def create(
        self,
        name: str,
        specialization: str | None = None,
        working_hours: str | None = None,
        days_off: str | None = None,
    ) -> Master:
        master = Master(
            name=name,
            specialization=specialization,
            working_hours=working_hours,
            days_off=days_off,
        )
        self._session.add(master)
        await self._session.flush()
        return master


class BookingRepository:
    """CRUD operations for bookings."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        user_vk_id: int,
        service_id: int,
        booking_date,
        booking_time,
        client_phone: str,
        master_id: int | None = None,
        client_name: str | None = None,
        notes: str | None = None,
    ) -> Booking:
        booking = Booking(
            user_vk_id=user_vk_id,
            service_id=service_id,
            master_id=master_id,
            booking_date=booking_date,
            booking_time=booking_time,
            client_phone=client_phone,
            client_name=client_name,
            notes=notes,
            status=BookingStatus.PENDING,
        )
        self._session.add(booking)
        await self._session.flush()
        return booking

    async def get_booked_times(self, booking_date, master_id: int | None = None) -> list:
        conditions = [
            Booking.booking_date == booking_date,
            Booking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED]),
        ]
        if master_id is not None:
            conditions.append(Booking.master_id == master_id)

        result = await self._session.execute(
            select(Booking.booking_time).where(and_(*conditions))
        )
        return list(result.scalars().all())

    async def list_active_intervals(
        self, booking_date, exclude_booking_id: int | None = None
    ) -> list[tuple]:
        """Return (start_time, duration_minutes, master_id) of pending/confirmed bookings on a date."""
        conditions = [
            Booking.booking_date == booking_date,
            Booking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED]),
        ]
        if exclude_booking_id is not None:
            conditions.append(Booking.id != exclude_booking_id)
        result = await self._session.execute(
            select(Booking.booking_time, Service.duration_minutes, Booking.master_id)
            .join(Service, Booking.service_id == Service.id)
            .where(*conditions)
        )
        return [tuple(row) for row in result.all()]

    async def list_active_for_user(self, user_vk_id: int) -> list[Booking]:
        """Pending/confirmed bookings of a user with service and master loaded."""
        result = await self._session.execute(
            select(Booking)
            .where(
                Booking.user_vk_id == user_vk_id,
                Booking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED]),
            )
            .options(selectinload(Booking.service), selectinload(Booking.master))
            .order_by(Booking.booking_date, Booking.booking_time)
        )
        return list(result.scalars().all())

    async def get_with_relations(self, booking_id: int) -> Booking | None:
        result = await self._session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.service), selectinload(Booking.master))
        )
        return result.scalar_one_or_none()

    async def list_by_user(self, user_vk_id: int) -> list[Booking]:
        result = await self._session.execute(
            select(Booking)
            .where(Booking.user_vk_id == user_vk_id)
            .order_by(Booking.booking_date.desc(), Booking.booking_time.desc())
        )
        return list(result.scalars().all())


class AdministratorRepository:
    """CRUD operations for administrators."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self) -> list[Administrator]:
        result = await self._session.execute(
            select(Administrator).where(Administrator.is_active.is_(True))
        )
        return list(result.scalars().all())

    async def get_by_vk_id(self, vk_id: int) -> Administrator | None:
        result = await self._session.execute(
            select(Administrator).where(Administrator.vk_id == vk_id)
        )
        return result.scalar_one_or_none()

    async def create(self, vk_id: int, name: str | None = None) -> Administrator:
        admin = Administrator(vk_id=vk_id, name=name)
        self._session.add(admin)
        await self._session.flush()
        return admin

    async def is_admin(self, vk_id: int) -> bool:
        admin = await self.get_by_vk_id(vk_id)
        return admin is not None and admin.is_active


class DialogRepository:
    """Conversation history and admin transfer tracking."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_message(self, user_vk_id: int, role: str, content: str) -> DialogMessage:
        message = DialogMessage(user_vk_id=user_vk_id, role=role, content=content)
        self._session.add(message)
        await self._session.flush()
        return message

    async def get_recent_messages(self, user_vk_id: int, limit: int = 20) -> list[DialogMessage]:
        result = await self._session.execute(
            select(DialogMessage)
            .where(DialogMessage.user_vk_id == user_vk_id)
            .order_by(DialogMessage.created_at.desc())
            .limit(limit)
        )
        messages = list(result.scalars().all())
        messages.reverse()
        return messages

    async def create_transfer(self, user_vk_id: int, reason: str | None = None) -> AdminTransfer:
        existing = await self._session.execute(
            select(AdminTransfer).where(
                AdminTransfer.user_vk_id == user_vk_id,
                AdminTransfer.is_active.is_(True),
            )
        )
        transfer = existing.scalar_one_or_none()
        if transfer is not None:
            if reason:
                transfer.reason = reason
            return transfer

        transfer = AdminTransfer(user_vk_id=user_vk_id, reason=reason, is_active=True)
        self._session.add(transfer)
        await self._session.flush()
        return transfer

    async def get_active_transfer(self, user_vk_id: int) -> AdminTransfer | None:
        result = await self._session.execute(
            select(AdminTransfer)
            .where(
                AdminTransfer.user_vk_id == user_vk_id,
                AdminTransfer.is_active.is_(True),
            )
            .order_by(AdminTransfer.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def is_admin_engaged(self, user_vk_id: int) -> bool:
        transfer = await self.get_active_transfer(user_vk_id)
        return transfer is not None and transfer.admin_engaged

    async def engage_admin(self, user_vk_id: int, admin_vk_id: int | None = None) -> str:
        """Mark that an admin took the dialog.

        Returns "ok", "no_transfer", "already_yours" or "taken".
        """
        transfer = await self.get_active_transfer(user_vk_id)
        if transfer is None:
            return "no_transfer"
        if transfer.admin_engaged:
            if admin_vk_id is not None and transfer.admin_vk_id not in (None, admin_vk_id):
                return "taken"
            if transfer.admin_vk_id == admin_vk_id:
                return "already_yours"
        transfer.admin_engaged = True
        transfer.admin_vk_id = admin_vk_id
        await self._session.flush()
        return "ok"

    async def has_active_transfer(self, user_vk_id: int) -> bool:
        return await self.get_active_transfer(user_vk_id) is not None

    async def resolve_transfer(self, user_vk_id: int) -> AdminTransfer | None:
        """Close the active transfer. Returns the closed transfer or None."""
        transfer = await self.get_active_transfer(user_vk_id)
        if transfer:
            transfer.is_active = False
            transfer.admin_engaged = False
            transfer.resolved_at = datetime.now(timezone.utc)
            await self._session.flush()
        return transfer

    async def delete_history(self, user_vk_id: int) -> None:
        await self._session.execute(
            delete(DialogMessage).where(DialogMessage.user_vk_id == user_vk_id)
        )


class ConsentRepository:
    """Personal data processing consents."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def has(self, user_vk_id: int) -> bool:
        result = await self._session.execute(
            select(ClientConsent.id).where(ClientConsent.user_vk_id == user_vk_id)
        )
        return result.scalar_one_or_none() is not None

    async def give(self, user_vk_id: int, policy_url: str | None) -> None:
        if await self.has(user_vk_id):
            return
        self._session.add(ClientConsent(user_vk_id=user_vk_id, policy_url=policy_url or None))
        await self._session.flush()

    async def revoke(self, user_vk_id: int) -> bool:
        result = await self._session.execute(
            delete(ClientConsent).where(ClientConsent.user_vk_id == user_vk_id)
        )
        return bool(result.rowcount)


class ProcessedMessageRepository:
    """Claim VK messages so only one bot instance replies."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_claim(self, peer_id: int, vk_message_id: int) -> bool:
        """Return True if this process should handle the message."""
        try:
            self._session.add(
                ProcessedVkMessage(peer_id=peer_id, vk_message_id=vk_message_id)
            )
            await self._session.flush()
            return True
        except IntegrityError:
            await self._session.rollback()
            return False
