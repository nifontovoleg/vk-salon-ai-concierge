"""Rule-based replies from database when LLM is unavailable."""

from __future__ import annotations

import json
from datetime import timedelta

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
        self._tools = ToolExecutor(session, user_vk_id, settings=settings)

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

    @staticmethod
    def classify(text: str) -> str | None:
        """Detect a known free-text intent by keywords."""
        lowered = text.lower().strip()
        if not lowered:
            return None
        if any(word in lowered for word in ("привет", "здравств", "добрый", "хай", "hello")):
            return "greeting"
        if any(
            phrase in lowered
            for phrase in ("мои запис", "моя запис", "отменить запис", "отмените запис",
                           "перенести запис", "перенесите запис", "отменить мою", "перенести мою")
        ):
            return "my_bookings"
        if any(word in lowered for word in ("запис", "запишите", "хочу на", "забронир")):
            return "book"
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
            return "services"
        if any(
            word in lowered
            for word in ("свобод", "слот", "когда можно", "свободное время", "ближайш")
        ):
            return "slots"
        if any(word in lowered for word in ("мастер", "кто стриж", "специалист")):
            return "masters"
        if any(word in lowered for word in ("адрес", "где вы", "как добраться", "находитесь")):
            return "address"
        return None

    async def try_reply(self, text: str, allowed: set[str] | None = None) -> str | None:
        """Return a reply for known free-text intents, or None if LLM is needed."""
        intent = self.classify(text)
        if intent is None or (allowed is not None and intent not in allowed):
            return None

        if intent == "greeting":
            return (
                f"Здравствуйте! Я консьерж салона «{self._settings.salon_name}».\n"
                "Помогу записаться, расскажу об услугах и ценах.\n"
                "Выберите кнопку ниже или напишите, что вас интересует."
            )
        if intent == "my_bookings":
            return "Ваши записи — по кнопке «Мои записи»: там можно отменить или перенести запись."
        if intent == "book":
            services = await self._format_services(short=True)
            return f"📅 {BOOKING_HINT}\n\n{services}"
        if intent == "services":
            return await self._format_services()
        if intent == "slots":
            return await self._format_slots()
        if intent == "masters":
            return await self._format_masters()
        return self.contacts_text()

    def contacts_text(self) -> str:
        unknown = "уточняйте у администратора"
        return (
            f"📍 {self._settings.salon_name}\n"
            f"Адрес: {self._settings.salon_address or unknown}\n"
            f"Телефон: {self._settings.salon_phone or unknown}\n"
            f"Часы работы: {self._settings.salon_working_hours or unknown}"
        )

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
            hours = master.get("working_hours") or "по часам салона"
            lines.append(f"• {master['name']} — {spec} ({hours})")
        return "\n".join(lines)

    async def _format_slots(self) -> str:
        schedule = self._tools.schedule
        today = schedule.today()
        tomorrow = today + timedelta(days=1)
        lines = ["🕐 Свободное время:"]

        for day in (today, tomorrow):
            label = "Сегодня" if day == today else "Завтра"
            if schedule.window(day) is None:
                lines.append(f"{label} ({day.strftime('%d.%m')}): выходной")
                continue
            free = await self._tools.free_slots(day, schedule.step)
            slots = [t.strftime("%H:%M") for t in free]
            if slots:
                preview = ", ".join(slots[:8])
                extra = f" (+{len(slots) - 8})" if len(slots) > 8 else ""
                lines.append(f"{label} ({day.strftime('%d.%m')}): {preview}{extra}")
            else:
                lines.append(f"{label} ({day.strftime('%d.%m')}): нет свободных слотов")

        lines.append("\nДля записи напишите услугу, дату, время и телефон.")
        return "\n".join(lines)
