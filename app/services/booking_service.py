"""Booking business logic.

- создание и перенос записи без гонок (блокировка дня + уникальный индекс);
- отмена и перенос клиентом своих будущих записей;
- подтверждение/отмена администратором;
- тексты уведомлений.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Booking, BookingDayLock, BookingStatus, Master
from app.repositories import BookingRepository, MasterRepository, ServiceRepository
from app.services.schedule import Schedule, is_free, minutes_of

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (BookingStatus.PENDING, BookingStatus.CONFIRMED)
LOCK_RETRIES = 6

STATUS_LABELS = {
    BookingStatus.PENDING: "ожидает подтверждения",
    BookingStatus.CONFIRMED: "подтверждена",
    BookingStatus.CANCELLED: "отменена",
    BookingStatus.COMPLETED: "завершена",
}


@dataclass(frozen=True)
class MasterInfo:
    """Plain copy of a master row (safe to use after commit/rollback)."""

    id: int
    name: str
    specialization: str | None = None
    working_hours: str | None = None
    days_off: str | None = None

    @classmethod
    def of(cls, master: Master) -> "MasterInfo":
        return cls(master.id, master.name, master.specialization, master.working_hours, master.days_off)


@dataclass
class BookingResult:
    ok: bool
    booking_id: int | None = None
    error: str | None = None
    nearest: list[tuple[date, time]] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    old_when: str | None = None

    def nearest_labels(self) -> list[str]:
        return [f"{d.strftime('%d.%m')} {t.strftime('%H:%M')}" for d, t in self.nearest]

    def as_tool(self) -> dict:
        if self.ok:
            return {"success": True, "booking_id": self.booking_id, **self.details}
        out: dict = {"success": False, "error": self.error}
        if self.nearest:
            out["nearest_free_slots"] = [f"{d.isoformat()} {t.strftime('%H:%M')}" for d, t in self.nearest]
        return out


def when(day: date, start: time) -> str:
    return f"{day.strftime('%d.%m.%Y')} в {start.strftime('%H:%M')}"


def _when(booking: Booking) -> str:
    return when(booking.booking_date, booking.booking_time)


def format_admin_notification(booking: Booking, header: str | None = None) -> str:
    service = booking.service
    master = booking.master.name if booking.master else "любой свободный"
    client = booking.client_name or "имя не указано"
    title = header or f"Новая запись #{booking.id} (ожидает подтверждения)"
    return (
        f"{title}\n"
        f"Услуга: {service.name} — {service.price} ₽, {service.duration_minutes} мин\n"
        f"Мастер: {master}\n"
        f"Когда: {_when(booking)}\n"
        f"Клиент: {client}, {booking.client_phone}, vk.com/id{booking.user_vk_id}\n"
        f"Статус: {STATUS_LABELS.get(booking.status, booking.status)}"
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


def describe_booking(booking: Booking) -> str:
    master = f", мастер {booking.master.name}" if booking.master else ""
    status = STATUS_LABELS.get(booking.status, booking.status)
    return f"#{booking.id} {booking.service.name}, {_when(booking)}{master} — {status}"


class BookingService:
    """High-level booking operations."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._session = session
        self.settings = settings or get_settings()
        self.schedule = Schedule(self.settings)
        self._bookings = BookingRepository(session)
        self._services = ServiceRepository(session)
        self._masters = MasterRepository(session)

    # ----------------------------------------------------------- masters/slots

    async def masters(self) -> list[MasterInfo]:
        return [MasterInfo.of(m) for m in await self._masters.list_active()]

    async def find_master(self, name: str) -> MasterInfo | None:
        master = await self._masters.get_by_name(name)
        return MasterInfo.of(master) if master and master.is_active else None

    async def _candidates(self, master: MasterInfo | None) -> tuple[list[MasterInfo | None], bool]:
        masters = await self.masters()
        if master is not None:
            return [master], True
        return (list(masters) if masters else [None]), bool(masters)

    async def free_slots(
        self,
        day: date,
        duration: int,
        master_id: int | None = None,
        exclude_booking_id: int | None = None,
    ) -> dict[time, int | None]:
        """Map free start time -> master id who can take it (None if the salon has no masters)."""
        masters = await self.masters()
        has_masters = bool(masters)
        if master_id is not None:
            candidates: list[MasterInfo | None] = [m for m in masters if m.id == master_id]
        elif has_masters:
            candidates = list(masters)
        else:
            candidates = [None]

        busy = await self._bookings.list_active_intervals(day, exclude_booking_id)
        result: dict[time, int | None] = {}
        for candidate in candidates:
            candidate_id = candidate.id if candidate else None
            for start in self.schedule.grid(day, duration, master=candidate):
                if start not in result and is_free(start, duration, busy, candidate_id, has_masters):
                    result[start] = candidate_id
        return dict(sorted(result.items()))

    async def nearest_slots(
        self,
        day: date,
        start: time,
        duration: int,
        master_id: int | None = None,
        exclude_booking_id: int | None = None,
        limit: int = 3,
        days_ahead: int = 14,
    ) -> list[tuple[date, time]]:
        """Free slots closest to the requested time: same day first, then the following days."""
        day = max(day, self.schedule.today())
        free = await self.free_slots(day, duration, master_id, exclude_booking_id)
        target = minutes_of(start)
        same_day = sorted(free, key=lambda t: abs(minutes_of(t) - target))[:limit]
        result = [(day, t) for t in sorted(same_day)]
        current = day
        for _ in range(days_ahead):
            if len(result) >= limit:
                break
            current += timedelta(days=1)
            for slot in await self.free_slots(current, duration, master_id, exclude_booking_id):
                result.append((current, slot))
                if len(result) >= limit:
                    break
        return result

    async def upcoming_slots(
        self,
        duration: int,
        master_id: int | None = None,
        exclude_booking_id: int | None = None,
        limit: int = 8,
        per_day: int = 3,
        days_ahead: int = 14,
    ) -> list[tuple[date, time]]:
        result: list[tuple[date, time]] = []
        current = self.schedule.today()
        for _ in range(days_ahead + 1):
            free = await self.free_slots(current, duration, master_id, exclude_booking_id)
            for slot in list(free)[:per_day]:
                result.append((current, slot))
                if len(result) >= limit:
                    return result
            current += timedelta(days=1)
        return result

    # ---------------------------------------------------------------- locking

    async def _lock_day(self, day: date) -> None:
        """Take the per-date write lock (first statement of the transaction is a write).

        PostgreSQL: строка booking_day_locks блокируется UPDATE до конца транзакции
        (как SELECT ... FOR UPDATE). SQLite: первая запись берёт блокировку записи
        всей базы (как BEGIN IMMEDIATE).
        """
        dialect = self._session.bind.dialect.name if self._session.bind else ""
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(BookingDayLock).values(booking_date=day, version=0)
            await self._session.execute(stmt.on_conflict_do_nothing(index_elements=["booking_date"]))
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            stmt = sqlite_insert(BookingDayLock).values(booking_date=day, version=0)
            await self._session.execute(stmt.on_conflict_do_nothing(index_elements=["booking_date"]))
        else:
            exists = await self._session.execute(
                select(BookingDayLock.booking_date).where(BookingDayLock.booking_date == day)
            )
            if exists.scalar_one_or_none() is None:
                self._session.add(BookingDayLock(booking_date=day, version=0))
                await self._session.flush()
        await self._session.execute(
            update(BookingDayLock)
            .where(BookingDayLock.booking_date == day)
            .values(version=BookingDayLock.version + 1)
        )

    async def _locked(self, day: date, write):
        """Run `write()` under the day lock in its own short transaction.

        Returns ("ok", value) | ("taken", None) | ("busy", None).
        `write()` returns None when the slot turned out to be busy.
        """
        for attempt in range(LOCK_RETRIES):
            await self._session.commit()
            try:
                await self._lock_day(day)
                value = await write()
                if value is None:
                    await self._session.rollback()
                    return "taken", None
                await self._session.commit()
                return "ok", value
            except IntegrityError:
                await self._session.rollback()
                return "taken", None
            except OperationalError as exc:
                await self._session.rollback()
                logger.warning("Booking lock attempt %s failed: %s", attempt + 1, exc)
                await asyncio.sleep(0.05 * (attempt + 1) + random.random() * 0.05)
        return "busy", None

    # ---------------------------------------------------------------- create

    async def create(
        self,
        user_vk_id: int,
        service_name: str,
        day: date,
        start: time,
        phone: str,
        client_name: str | None = None,
        master_name: str | None = None,
    ) -> BookingResult:
        service = await self._services.get_by_name(service_name)
        if service is None:
            return BookingResult(False, error=f"Услуга '{service_name}' не найдена")
        service_id, service_title, duration = service.id, service.name, service.duration_minutes

        master: MasterInfo | None = None
        if master_name:
            master = await self.find_master(master_name)
            if master is None:
                return BookingResult(False, error=f"Мастер '{master_name}' не найден")
        candidates, _ = await self._candidates(master)
        master_id = master.id if master else None

        problem = self.schedule.validate_start(day, start, duration, candidates)
        if problem:
            return BookingResult(
                False, error=problem, nearest=await self.nearest_slots(day, start, duration, master_id)
            )

        async def write():
            free = await self.free_slots(day, duration, master_id)
            if start not in free:
                return None
            booking = await self._bookings.create(
                user_vk_id=user_vk_id,
                service_id=service_id,
                master_id=free[start],
                booking_date=day,
                booking_time=start,
                client_phone=phone,
                client_name=client_name,
            )
            return booking.id, free[start]

        outcome, value = await self._locked(day, write)
        if outcome != "ok":
            return await self._taken(outcome, day, start, duration, master_id)

        booking_id, assigned_id = value
        assigned = next((m.name for m in await self.masters() if m.id == assigned_id), None)
        return BookingResult(
            True,
            booking_id=booking_id,
            details={
                "service": service_title,
                "master": assigned,
                "date": day.isoformat(),
                "time": start.strftime("%H:%M"),
                "status": "ожидает подтверждения администратора",
            },
        )

    async def _taken(self, outcome, day, start, duration, master_id, exclude_id=None) -> BookingResult:
        nearest = await self.nearest_slots(day, start, duration, master_id, exclude_id)
        if outcome == "busy":
            error = "Не удалось сохранить запись: база занята, попробуйте ещё раз через минуту"
        else:
            error = "Выбранное время уже занято"
        return BookingResult(False, error=error, nearest=nearest)

    # ------------------------------------------------------- client actions

    async def list_upcoming(self, user_vk_id: int) -> list[Booking]:
        now = self.schedule.now()
        return [
            b
            for b in await self._bookings.list_active_for_user(user_vk_id)
            if datetime.combine(b.booking_date, b.booking_time) > now
        ]

    async def _own_changeable(self, user_vk_id: int, booking_id: int) -> tuple[Booking | None, str | None]:
        booking = await self._bookings.get_with_relations(booking_id)
        if booking is None or booking.user_vk_id != user_vk_id:
            return None, f"Запись #{booking_id} не найдена среди ваших"
        if booking.status not in ACTIVE_STATUSES:
            return None, f"Запись #{booking_id} уже {STATUS_LABELS.get(booking.status, booking.status)}"
        starts = datetime.combine(booking.booking_date, booking.booking_time)
        now = self.schedule.now()
        if starts <= now:
            return None, f"Запись #{booking_id} уже прошла"
        cutoff = self.settings.cancel_min_hours
        if cutoff and starts - now < timedelta(hours=cutoff):
            phone = f": {self.settings.salon_phone}" if self.settings.salon_phone else ""
            hours = f"{cutoff:g}"
            return None, (
                f"До записи #{booking_id} меньше {hours} ч — отменить или перенести её онлайн нельзя. "
                f"Позвоните в салон{phone}"
            )
        return booking, None

    async def check_changeable(self, user_vk_id: int, booking_id: int) -> str | None:
        _, error = await self._own_changeable(user_vk_id, booking_id)
        return error

    async def cancel_by_client(self, user_vk_id: int, booking_id: int) -> BookingResult:
        booking, error = await self._own_changeable(user_vk_id, booking_id)
        if error:
            return BookingResult(False, error=error)
        old = describe_booking(booking)
        await self._session.execute(
            update(Booking).where(Booking.id == booking_id).values(status=BookingStatus.CANCELLED)
        )
        await self._session.commit()
        return BookingResult(True, booking_id=booking_id, details={"cancelled": old}, old_when=_when(booking))

    async def reschedule(
        self,
        user_vk_id: int,
        booking_id: int,
        day: date,
        start: time,
        master_name: str | None = None,
    ) -> BookingResult:
        booking, error = await self._own_changeable(user_vk_id, booking_id)
        if error:
            return BookingResult(False, error=error)
        old_when = _when(booking)
        duration = booking.service.duration_minutes
        service_title = booking.service.name
        current_master_id = booking.master_id

        master: MasterInfo | None = None
        if master_name:
            master = await self.find_master(master_name)
            if master is None:
                return BookingResult(False, error=f"Мастер '{master_name}' не найден")
        elif current_master_id is not None:
            master = next((m for m in await self.masters() if m.id == current_master_id), None)
        candidates, _ = await self._candidates(master)
        master_id = master.id if master else None

        problem = self.schedule.validate_start(day, start, duration, candidates)
        if problem:
            nearest = await self.nearest_slots(day, start, duration, master_id, booking_id)
            return BookingResult(False, error=problem, nearest=nearest)

        async def write():
            free = await self.free_slots(day, duration, master_id, exclude_booking_id=booking_id)
            if start not in free:
                return None
            result = await self._session.execute(
                update(Booking)
                .where(
                    Booking.id == booking_id,
                    Booking.user_vk_id == user_vk_id,
                    Booking.status.in_(ACTIVE_STATUSES),
                )
                .values(
                    booking_date=day,
                    booking_time=start,
                    master_id=free[start],
                    status=BookingStatus.PENDING,
                )
            )
            return free[start] if result.rowcount else None

        outcome, assigned_id = await self._locked(day, write)
        if outcome != "ok":
            return await self._taken(outcome, day, start, duration, master_id, booking_id)
        assigned = next((m.name for m in await self.masters() if m.id == assigned_id), None)
        return BookingResult(
            True,
            booking_id=booking_id,
            old_when=old_when,
            details={
                "service": service_title,
                "master": assigned,
                "date": day.isoformat(),
                "time": start.strftime("%H:%M"),
                "was": old_when,
                "status": "ожидает подтверждения администратора",
            },
        )

    # -------------------------------------------------------------- admin

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


async def sync_master_schedules(session: AsyncSession, settings: Settings) -> list[str]:
    """Apply MASTER_SCHEDULES / MASTER_DAYS_OFF from .env to masters in the DB.

    Returns warnings (unknown master, unparsed hours). Masters not mentioned keep their DB values.
    """
    from app.services.schedule import parse_hours_strict

    repo = MasterRepository(session)
    warnings: list[str] = []
    for name, hours in settings.master_schedule_map.items():
        master = await repo.get_by_name(name)
        if master is None:
            warnings.append(f"MASTER_SCHEDULES: мастер «{name}» не найден в базе")
            continue
        if hours and not parse_hours_strict(hours):
            warnings.append(f"MASTER_SCHEDULES: часы мастера «{name}» не распознаны: {hours}")
            continue
        master.working_hours = hours or None
    for name, days in settings.master_days_off_map.items():
        master = await repo.get_by_name(name)
        if master is None:
            warnings.append(f"MASTER_DAYS_OFF: мастер «{name}» не найден в базе")
            continue
        master.days_off = days or None
    await session.flush()
    for warning in warnings:
        logger.warning(warning)
    return warnings
