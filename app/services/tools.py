"""LLM tool definitions and execution."""

from __future__ import annotations

import json
import logging
from datetime import date, time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.privacy import PHONE_LABEL, extract_phone

from app.repositories import (
    AdministratorRepository,
    BookingRepository,
    DialogRepository,
    MasterRepository,
    ServiceRepository,
)

from app.services.schedule import Schedule, is_free

logger = logging.getLogger(__name__)

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
                    "service_name": {
                        "type": "string",
                        "description": "Название услуги, чтобы учесть её длительность (необязательно)",
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
            "description": (
                "Создать запись клиента на услугу. Запись получает статус «ожидает подтверждения», "
                "администратор подтверждает её отдельно"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service_name": {"type": "string", "description": "Название услуги"},
                    "date": {"type": "string", "description": "Дата YYYY-MM-DD"},
                    "time": {"type": "string", "description": "Время HH:MM"},
                    "phone": {
                        "type": "string",
                        "description": f"Телефон клиента; если в тексте он скрыт меткой {PHONE_LABEL}, передай её как есть",
                    },
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


class ToolExecutor:
    """Executes LLM tool calls against the database."""

    def __init__(
        self,
        session: AsyncSession,
        user_vk_id: int,
        settings: Settings | None = None,
        known_phone: str | None = None,
    ) -> None:
        self._session = session
        self._user_vk_id = user_vk_id
        self._settings = settings or get_settings()
        self._known_phone = known_phone
        self.schedule = Schedule(self._settings)
        self._services = ServiceRepository(session)
        self._masters = MasterRepository(session)
        self._bookings = BookingRepository(session)
        self._admins = AdministratorRepository(session)
        self._dialogs = DialogRepository(session)
        self.transfer_requested = False
        self.transfer_unavailable = False
        self.transfer_reason: str | None = None
        self.created_booking_ids: list[int] = []

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
        if not isinstance(arguments, dict):
            return json.dumps({"error": "Аргументы должны быть JSON-объектом"}, ensure_ascii=False)
        try:
            result = await handler(arguments)
            return json.dumps(result, ensure_ascii=False, default=str)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Tool %s got bad arguments: %s", name, exc)
            return json.dumps({"error": f"Некорректные аргументы: {exc}"}, ensure_ascii=False)
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

    async def _resolve_master(self, name: str | None):
        if not name:
            return None, None
        master = await self._masters.get_by_name(name)
        if master is None:
            return None, {"error": f"Мастер '{name}' не найден"}
        return master, None

    async def free_slots(
        self, booking_date: date, duration: int, master_id: int | None = None
    ) -> dict[time, int | None]:
        """Map free start time -> master id that can take it (None if salon has no masters)."""
        masters = await self._masters.list_active()
        has_masters = bool(masters)
        if master_id is not None:
            candidates: list[int | None] = [master_id]
        elif has_masters:
            candidates = [m.id for m in masters]
        else:
            candidates = [None]

        busy = await self._bookings.list_active_intervals(booking_date)
        result: dict[time, int | None] = {}
        for start in self.schedule.grid(booking_date, duration):
            for candidate in candidates:
                if is_free(start, duration, busy, candidate, has_masters):
                    result[start] = candidate
                    break
        return result

    async def _get_available_slots(self, args: dict[str, Any]) -> dict[str, Any]:
        booking_date = date.fromisoformat(str(args["date"]))
        if booking_date < self.schedule.today():
            return {"error": "Дата в прошлом", "today": str(self.schedule.today())}

        master, error = await self._resolve_master(args.get("master_name"))
        if error:
            return error

        duration = self.schedule.step
        if args.get("service_name"):
            service = await self._services.get_by_name(args["service_name"])
            if service is None:
                return {"error": f"Услуга '{args['service_name']}' не найдена"}
            duration = service.duration_minutes

        if self.schedule.window(booking_date) is None:
            return {"date": str(booking_date), "available_slots": [], "note": "В этот день салон не работает"}

        free = await self.free_slots(booking_date, duration, master.id if master else None)
        return {
            "date": str(booking_date),
            "working_hours": self.schedule.describe_day(booking_date),
            "duration_minutes": duration,
            "available_slots": [t.strftime("%H:%M") for t in free],
        }

    def _resolve_phone(self, raw: Any) -> str | None:
        value = str(raw or "").strip()
        if sum(ch.isdigit() for ch in value) >= 10:
            return extract_phone(value) or value
        return self._known_phone

    async def _create_booking(self, args: dict[str, Any]) -> dict[str, Any]:
        service = await self._services.get_by_name(args["service_name"])
        if service is None:
            return {"error": f"Услуга '{args['service_name']}' не найдена"}

        master, error = await self._resolve_master(args.get("master_name"))
        if error:
            return error

        phone = self._resolve_phone(args.get("phone"))
        if not phone:
            return {"error": "Нужен номер телефона клиента: попроси клиента написать его"}

        booking_date = date.fromisoformat(str(args["date"]))
        booking_time = time.fromisoformat(str(args["time"]))
        duration = service.duration_minutes

        problem = self.schedule.validate_start(booking_date, booking_time, duration)
        if problem:
            return {"error": problem}

        free = await self.free_slots(booking_date, duration, master.id if master else None)
        if booking_time not in free:
            return {"error": "Выбранное время уже занято"}
        master_id = free[booking_time]

        booking = await self._bookings.create(
            user_vk_id=self._user_vk_id,
            service_id=service.id,
            master_id=master_id,
            booking_date=booking_date,
            booking_time=booking_time,
            client_phone=phone,
            client_name=args.get("client_name"),
        )
        self.created_booking_ids.append(booking.id)
        assigned = None
        if master_id is not None:
            assigned_master = master or await self._masters.get_by_id(master_id)
            assigned = assigned_master.name if assigned_master else None
        return {
            "success": True,
            "booking_id": booking.id,
            "service": service.name,
            "master": assigned,
            "date": str(booking_date),
            "time": booking_time.strftime("%H:%M"),
            "status": "ожидает подтверждения администратора",
        }

    async def _transfer_to_admin(self, args: dict[str, Any]) -> dict[str, Any]:
        reason = args.get("reason") or "Запрос клиента"
        admins = [a for a in await self._admins.list_active() if a.vk_id != self._user_vk_id]
        if not admins:
            logger.warning("transfer_to_admin: no active administrators (user %s)", self._user_vk_id)
            self.transfer_unavailable = True
            return {"success": False, "error": "Администраторы не подключены, передать диалог нельзя"}
        await self._dialogs.create_transfer(self._user_vk_id, reason)
        self.transfer_requested = True
        self.transfer_reason = reason
        return {"success": True, "message": "Диалог передан администратору"}
