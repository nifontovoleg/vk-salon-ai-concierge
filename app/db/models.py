"""SQLAlchemy ORM models."""

from datetime import date, datetime, time
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""


class BookingStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


ACTIVE_STATUS_SQL = "status IN ('pending', 'confirmed')"
ACTIVE_BOOKING_INDEX = "uq_bookings_active_master_start"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class ServiceCategory(StrEnum):
    HAIRCUT = "haircut"
    COLORING = "coloring"
    STYLING = "styling"
    MANICURE = "manicure"
    OTHER = "other"


class Service(Base):
    """Salon service with pricing."""

    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[int] = mapped_column(Integer, nullable=False, comment="Price in rubles")
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default=ServiceCategory.OTHER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    bookings: Mapped[list["Booking"]] = relationship(back_populates="service")


class Master(Base):
    """Salon master / stylist."""

    __tablename__ = "masters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    specialization: Mapped[str | None] = mapped_column(String(255), nullable=True)
    working_hours: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="«Пн-Ср 10:00-18:00»; пусто — часы салона"
    )
    days_off: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="«2026-11-02, 2026-11-10..2026-11-15»"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    bookings: Mapped[list["Booking"]] = relationship(back_populates="master")


class Booking(Base):
    """Client appointment."""

    __tablename__ = "bookings"
    __table_args__ = (
        # Страховка на уровне БД: у мастера не может быть двух активных записей с одним началом.
        # Пересечения с разным началом исключает блокировка дня в BookingService.
        Index(
            ACTIVE_BOOKING_INDEX,
            "master_id",
            "booking_date",
            "booking_time",
            unique=True,
            sqlite_where=text(ACTIVE_STATUS_SQL),
            postgresql_where=text(ACTIVE_STATUS_SQL),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_vk_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False)
    master_id: Mapped[int | None] = mapped_column(ForeignKey("masters.id"), nullable=True)
    booking_date: Mapped[date] = mapped_column(Date, nullable=False)
    booking_time: Mapped[time] = mapped_column(Time, nullable=False)
    client_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    client_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=BookingStatus.PENDING)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    service: Mapped["Service"] = relationship(back_populates="bookings")
    master: Mapped["Master | None"] = relationship(back_populates="bookings")


class Administrator(Base):
    """Human admin who can take over dialogs."""

    __tablename__ = "administrators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vk_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DialogMessage(Base):
    """Conversation history for context."""

    __tablename__ = "dialog_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_vk_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProcessedVkMessage(Base):
    """Tracks handled VK messages to prevent duplicate replies across bot instances."""

    __tablename__ = "processed_vk_messages"
    __table_args__ = (UniqueConstraint("peer_id", "vk_message_id", name="uq_vk_peer_message"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    peer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vk_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminTransfer(Base):
    """Tracks dialogs handed off to administrators."""

    __tablename__ = "admin_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_vk_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    admin_engaged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    admin_vk_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ClientConsent(Base):
    """Client consent to personal data processing (152-FZ)."""

    __tablename__ = "client_consents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_vk_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    policy_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    given_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BookingDayLock(Base):
    """One row per date; bookings for that date are written under its row lock."""

    __tablename__ = "booking_day_locks"

    booking_date: Mapped[date] = mapped_column(Date, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
