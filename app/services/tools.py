"""LLM tool definitions and execution."""

from __future__ import annotations

import json
import logging
from datetime import date, time, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.privacy import PHONE_LABEL, extract_phone

from app.repositories import AdministratorRepository, DialogRepository, ServiceRepository

from app.services.booking_service import BookingService

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
                        "description": (
                            "Телефон клиента; если в тексте он скрыт меткой вроде [ТЕЛЕФОН_1] "
                            f"или {PHONE_LABEL}, передай её как есть"
                        ),
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
            "name": "list_my_bookings",
            "description": "Будущие активные записи этого клиента (ожидают подтверждения или подтверждены)",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_booking",
            "description": "Отменить запись клиента. Перед вызовом уточни у клиента, какую запись отменить",
            "parameters": {
                "type": "object",
                "properties": {"booking_id": {"type": "integer", "description": "Номер записи"}},
                "required": ["booking_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_booking",
            "description": (
                "Перенести запись клиента на другое время из сетки. "
                "После переноса запись снова ждёт подтверждения администратора"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "booking_id": {"type": "integer", "description": "Номер записи"},
                    "date": {"type": "string", "description": "Новая дата YYYY-MM-DD"},
                    "time": {"type": "string", "description": "Новое время HH:MM"},
                    "master_name": {"type": "string", "description": "Другой мастер (необязательно)"},
                },
                "required": ["booking_id", "date", "time"],
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
        self.bookings = BookingService(session, self._settings)
        self.schedule = self.bookings.schedule
        self._services = ServiceRepository(session)
        self._admins = AdministratorRepository(session)
        self._dialogs = DialogRepository(session)
        self.transfer_requested = False
        self.transfer_unavailable = False
        self.transfer_reason: str | None = None
        # (booking_id, kind, note): kind = new | rescheduled | cancelled
        self.notifications: list[tuple[int, str, str | None]] = []

    @property
    def created_booking_ids(self) -> list[int]:
        return [booking_id for booking_id, kind, _ in self.notifications if kind == "new"]

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Run a tool by name and return JSON string result."""
        handlers = {
            "get_services": self._get_services,
            "get_masters": self._get_masters,
            "get_available_slots": self._get_available_slots,
            "create_booking": self._create_booking,
            "list_my_bookings": self._list_my_bookings,
            "cancel_booking": self._cancel_booking,
            "reschedule_booking": self._reschedule_booking,
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

    async def free_slots(self, booking_date: date, duration: int, master_id: int | None = None):
        return await self.bookings.free_slots(booking_date, duration, master_id)

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
        masters = await self.bookings.masters()
        today = self.schedule.today()
        return {
            "masters": [
                {
                    "id": m.id,
                    "name": m.name,
                    "specialization": m.specialization,
                    "working_hours": m.working_hours or "по часам салона",
                    "next_7_days": {
                        (today + timedelta(days=i)).isoformat(): self.schedule.describe_day(
                            today + timedelta(days=i), m
                        )
                        for i in range(7)
                    },
                }
                for m in masters
            ]
        }

    async def _get_available_slots(self, args: dict[str, Any]) -> dict[str, Any]:
        booking_date = date.fromisoformat(str(args["date"]))
        if booking_date < self.schedule.today():
            return {"error": "Дата в прошлом", "today": str(self.schedule.today())}

        master = None
        if args.get("master_name"):
            master = await self.bookings.find_master(args["master_name"])
            if master is None:
                return {"error": f"Мастер '{args['master_name']}' не найден"}

        duration = self.schedule.step
        if args.get("service_name"):
            service = await self._services.get_by_name(args["service_name"])
            if service is None:
                return {"error": f"Услуга '{args['service_name']}' не найдена"}
            duration = service.duration_minutes

        if self.schedule.window(booking_date, master) is None:
            who = f"Мастер {master.name}" if master else "Салон"
            return {"date": str(booking_date), "available_slots": [], "note": f"{who} в этот день не работает"}

        free = await self.bookings.free_slots(booking_date, duration, master.id if master else None)
        return {
            "date": str(booking_date),
            "working_hours": self.schedule.describe_day(booking_date, master),
            "duration_minutes": duration,
            "available_slots": [t.strftime("%H:%M") for t in free],
        }

    def _resolve_phone(self, raw: Any) -> str | None:
        value = str(raw or "").strip()
        if sum(ch.isdigit() for ch in value) >= 10:
            return extract_phone(value) or value
        return self._known_phone

    async def _create_booking(self, args: dict[str, Any]) -> dict[str, Any]:
        phone = self._resolve_phone(args.get("phone"))
        if not phone:
            return {"error": "Нужен номер телефона клиента: попроси клиента написать его"}
        result = await self.bookings.create(
            user_vk_id=self._user_vk_id,
            service_name=str(args["service_name"]),
            day=date.fromisoformat(str(args["date"])),
            start=time.fromisoformat(str(args["time"])),
            phone=phone,
            client_name=args.get("client_name"),
            master_name=args.get("master_name"),
        )
        if result.ok:
            self.notifications.append((result.booking_id, "new", None))
        return result.as_tool()

    async def _list_my_bookings(self, _: dict[str, Any]) -> dict[str, Any]:
        bookings = await self.bookings.list_upcoming(self._user_vk_id)
        return {
            "bookings": [
                {
                    "booking_id": b.id,
                    "service": b.service.name,
                    "master": b.master.name if b.master else None,
                    "date": b.booking_date.isoformat(),
                    "time": b.booking_time.strftime("%H:%M"),
                    "status": b.status,
                }
                for b in bookings
            ]
        }

    async def _cancel_booking(self, args: dict[str, Any]) -> dict[str, Any]:
        result = await self.bookings.cancel_by_client(self._user_vk_id, int(args["booking_id"]))
        if result.ok:
            self.notifications.append((result.booking_id, "cancelled", None))
        return result.as_tool()

    async def _reschedule_booking(self, args: dict[str, Any]) -> dict[str, Any]:
        result = await self.bookings.reschedule(
            self._user_vk_id,
            int(args["booking_id"]),
            date.fromisoformat(str(args["date"])),
            time.fromisoformat(str(args["time"])),
            master_name=args.get("master_name"),
        )
        if result.ok:
            self.notifications.append((result.booking_id, "rescheduled", result.old_when))
        return result.as_tool()

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
