"""LLM tool definitions and execution."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import (
    AdministratorRepository,
    BookingRepository,
    DialogRepository,
    MasterRepository,
    ServiceRepository,
)

logger = logging.getLogger(__name__)

WORK_START = time(10, 0)
WORK_END = time(20, 0)
SLOT_MINUTES = 60

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_services",
            "description": "Получить список услуг салона с ценами и длительностью",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_masters",
            "description": "Получить список мастеров салона",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_available_slots",
            "description": "Получить свободные слоты на указанную дату",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Дата в формате YYYY-MM-DD"},
                    "master_name": {
                        "type": "string",
                        "description": "Имя мастера (необязательно)",
                    },
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": "Создать запись клиента на услугу",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {"type": "string", "description": "Название услуги"},
                    "date": {"type": "string", "description": "Дата YYYY-MM-DD"},
                    "time": {"type": "string", "description": "Время HH:MM"},
                    "phone": {"type": "string", "description": "Телефон клиента"},
                    "master_name": {"type": "string", "description": "Имя мастера (необязательно)"},
                    "client_name": {"type": "string", "description": "Имя клиента (необязательно)"},
                },
                "required": ["service_name", "date", "time", "phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_to_admin",
            "description": "Передать диалог администратору салона",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Причина передачи"},
                },
                "required": ["reason"],
            },
        },
    },
]


def _generate_slots() -> list[time]:
    slots: list[time] = []
    current = datetime.combine(date.today(), WORK_START)
    end = datetime.combine(date.today(), WORK_END)
    while current < end:
        slots.append(current.time())
        current += timedelta(minutes=SLOT_MINUTES)
    return slots


class ToolExecutor:
    """Executes LLM tool calls against the database."""

    def __init__(self, session: AsyncSession, user_vk_id: int) -> None:
        self._session = session
        self._user_vk_id = user_vk_id
        self._services = ServiceRepository(session)
        self._masters = MasterRepository(session)
        self._bookings = BookingRepository(session)
        self._admins = AdministratorRepository(session)
        self._dialogs = DialogRepository(session)
        self.transfer_requested = False
        self.transfer_reason: str | None = None

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Run a tool by name and return JSON string result."""
        handlers = {
            "get_services": self._get_services,
            "get_masters": self._get_masters,
            "get_available_slots": self._get_available_slots,
            "create_booking": self._create_booking,
            "transfer_to_admin": self._transfer_to_admin,
        }
        handler = handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)
        try:
            result = await handler(arguments)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    async def _get_services(self, _: dict[str, Any]) -> dict[str, Any]:
        services = await self._services.list_active()
        return {
            "services": [
                {
                    "id": s.id,
                    "name": s.name,
                    "price": s.price,
                    "duration_minutes": s.duration_minutes,
                    "category": s.category,
                    "description": s.description,
                }
                for s in services
            ]
        }

    async def _get_masters(self, _: dict[str, Any]) -> dict[str, Any]:
        masters = await self._masters.list_active()
        return {
            "masters": [
                {"id": m.id, "name": m.name, "specialization": m.specialization}
                for m in masters
            ]
        }

    async def _get_available_slots(self, args: dict[str, Any]) -> dict[str, Any]:
        booking_date = date.fromisoformat(args["date"])
        master_id = None
        if args.get("master_name"):
            master = await self._masters.get_by_name(args["master_name"])
            if master is None:
                return {"error": f"Мастер '{args['master_name']}' не найден"}
            master_id = master.id

        booked = await self._bookings.get_booked_times(booking_date, master_id)
        booked_set = set(booked)
        available = [t.strftime("%H:%M") for t in _generate_slots() if t not in booked_set]
        return {"date": str(booking_date), "available_slots": available}

    async def _create_booking(self, args: dict[str, Any]) -> dict[str, Any]:
        service = await self._services.get_by_name(args["service_name"])
        if service is None:
            return {"error": f"Услуга '{args['service_name']}' не найдена"}

        master_id = None
        if args.get("master_name"):
            master = await self._masters.get_by_name(args["master_name"])
            if master is None:
                return {"error": f"Мастер '{args['master_name']}' не найден"}
            master_id = master.id

        booking_date = date.fromisoformat(args["date"])
        booking_time = time.fromisoformat(args["time"])

        booked = await self._bookings.get_booked_times(booking_date, master_id)
        if booking_time in booked:
            return {"error": "Выбранное время уже занято"}

        booking = await self._bookings.create(
            user_vk_id=self._user_vk_id,
            service_id=service.id,
            master_id=master_id,
            booking_date=booking_date,
            booking_time=booking_time,
            client_phone=args["phone"],
            client_name=args.get("client_name"),
        )
        return {
            "success": True,
            "booking_id": booking.id,
            "service": service.name,
            "date": str(booking_date),
            "time": booking_time.strftime("%H:%M"),
        }

    async def _transfer_to_admin(self, args: dict[str, Any]) -> dict[str, Any]:
        reason = args.get("reason", "Запрос клиента")
        await self._dialogs.create_transfer(self._user_vk_id, reason)
        self.transfer_requested = True
        self.transfer_reason = reason
        admins = await self._admins.list_active()
        return {
            "success": True,
            "message": "Диалог передан администратору",
            "admin_count": len(admins),
        }
