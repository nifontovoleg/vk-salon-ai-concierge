"""Rule-based replies from database when LLM is unavailable."""

from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.tools import ToolExecutor

BOOKING_HINT = (
    "Для записи напишите одним сообщением:\n"
    "• услугу (например, «Женская стрижка»)\n"
    "• дату (ДД.ММ.ГГГГ)\n"
    "• время (ЧЧ:ММ)\n"
    "• телефон\n"
    "• мастера — необязательно"
)


class FallbackService:
    """Quick answers using salon data without OpenRouter."""

    def __init__(self, session: AsyncSession, settings: Settings, user_vk_id: int) -> None:
        self._session = session
        self._settings = settings
        self._user_vk_id = user_vk_id
        self._tools = ToolExecutor(session, user_vk_id)

    async def reply_by_intent(self, intent: str) -> str:
        """Return a reply for a button intent."""
        if intent == "book":
            services = await self._format_services(short=True)
            return f"📅 Запись в салон «{self._settings.salon_name}»\n\n{BOOKING_HINT}\n\n{services}"
        if intent == "services":
            return await self._format_services()
        if intent == "slots":
            return await self._format_slots()
        return "Выберите действие на клавиатуре или напишите ваш вопрос."

    async def try_reply(self, text: str) -> str | None:
        """Return a reply for known free-text intents, or None if LLM is needed."""
        lowered = text.lower().strip()
        if not lowered:
            return None

        if any(word in lowered for word in ("привет", "здравств", "добрый", "хай", "hello")):
            return (
                f"Здравствуйте! Я консьерж салона «{self._settings.salon_name}».\n"
                "Помогу записаться, расскажу об услугах и ценах.\n"
                "Выберите кнопку ниже или напишите, что вас интересует."
            )

        if any(word in lowered for word in ("запис", "запишите", "хочу на", "забронир")):
            services = await self._format_services(short=True)
            return f"📅 {BOOKING_HINT}\n\n{services}"

        if any(
            phrase in lowered
            for phrase in (
                "услуги и цен",
                "услуги и стоим",
                "прайс",
                "сколько стоит",
                "расскажите про услуг",
                "что у вас есть",
            )
        ):
            return await self._format_services()

        if any(
            word in lowered
            for word in ("свобод", "слот", "когда можно", "свободное время", "ближайш")
        ):
            return await self._format_slots()

        if any(word in lowered for word in ("мастер", "кто стриж", "специалист")):
            return await self._format_masters()

        if any(word in lowered for word in ("адрес", "где вы", "как добраться", "находитесь")):
            return (
                f"📍 {self._settings.salon_name}\n"
                f"Адрес: {self._settings.salon_address or 'уточняйте у администратора'}\n"
                f"Телефон: {self._settings.salon_phone or 'уточняйте у администратора'}\n"
                f"Часы работы: {self._settings.salon_working_hours or 'уточняйте у администратора'}"
            )

        return None

    async def _format_services(self, short: bool = False) -> str:
        raw = await self._tools.execute("get_services", {})
        data = json.loads(raw)
        services = data.get("services", [])
        if not services:
            return "Список услуг пока пуст. Напишите «администратор» для уточнения."

        lines = ["💇 Услуги и цены:"]
        for item in services:
            lines.append(f"• {item['name']} — {item['price']} ₽ ({item['duration_minutes']} мин)")
            if not short and item.get("description"):
                lines.append(f"  {item['description']}")
        return "\n".join(lines)

    async def _format_masters(self) -> str:
        raw = await self._tools.execute("get_masters", {})
        data = json.loads(raw)
        masters = data.get("masters", [])
        if not masters:
            return "Информация о мастерах уточняется у администратора."

        lines = ["👤 Наши мастера:"]
        for master in masters:
            spec = master.get("specialization") or "универсал"
            lines.append(f"• {master['name']} — {spec}")
        return "\n".join(lines)

    async def _format_slots(self) -> str:
        today = date.today()
        tomorrow = today + timedelta(days=1)
        lines = ["🕐 Свободное время:"]

        for day in (today, tomorrow):
            raw = await self._tools.execute("get_available_slots", {"date": str(day)})
            data = json.loads(raw)
            slots = data.get("available_slots", [])
            label = "Сегодня" if day == today else "Завтра"
            if slots:
                preview = ", ".join(slots[:8])
                extra = f" (+{len(slots) - 8})" if len(slots) > 8 else ""
                lines.append(f"{label} ({day.strftime('%d.%m')}): {preview}{extra}")
            else:
                lines.append(f"{label} ({day.strftime('%d.%m')}): нет свободных слотов")

        lines.append("\nДля записи напишите услугу, дату, время и телефон.")
        return "\n".join(lines)
