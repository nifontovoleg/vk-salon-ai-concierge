"""Режим мастера-одиночки (SOLO_MODE).

- Новому клиенту сразу, без модели, отвечаем прайсом из базы и ближайшими окнами.
- Запись кнопками: услуга → время → телефон, без выбора мастера.
- Цены в ответах модели сверяются с прайсом в базе (guard_prices).
"""

from __future__ import annotations

import logging
import re
from datetime import date, time

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import SoloClient
from app.keyboards import consent_keyboard, solo_services_keyboard, solo_slots_keyboard
from app.privacy import extract_phone
from app.repositories import ServiceRepository
from app.schemas.message import DialogAction, DialogActionType, DialogResponse, IncomingMessage
from app.services.booking_service import BookingService, when

logger = logging.getLogger(__name__)

# «2500 ₽», «2 500 руб.», «от 1500р», «1500 рублей»
PRICE_RE = re.compile(r"(\d[\d\s\u00a0\u202f]{0,9}\d|\d)\s*(?:₽|руб\w*\.?|р\.|р\b)", re.IGNORECASE)

SOLO_PROMPT = """

Режим мастера-одиночки:
- В салоне работает один мастер, не спрашивай клиента про мастера и не предлагай выбор мастера.
- Цены называй ТОЛЬКО из get_services, ровно как там записаны. Никаких «примерно», «от», диапазонов и скидок.
- Если услуги нет в прайсе — прямо скажи, что её нет в прайсе, цену не называй и предложи позвать мастера (transfer_to_admin).
- Если клиент хочет записаться — сразу предложи ближайшие свободные окна через get_available_slots."""


def quoted_prices(text: str) -> set[int]:
    result = set()
    for match in PRICE_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(1))
        if digits:
            result.add(int(digits))
    return result


async def price_list_text(session: AsyncSession) -> str:
    services = await ServiceRepository(session).list_active()
    if not services:
        return "Прайс пока не заполнен — цены уточнит мастер."
    lines = ["Прайс:"] + [f"• {s.name} — {s.price} ₽ ({s.duration_minutes} мин)" for s in services]
    return "\n".join(lines)


async def guard_prices(session: AsyncSession, text: str) -> str:
    """Если модель назвала сумму, которой нет в прайсе, заменяем ответ прайсом из базы."""
    quoted = quoted_prices(text)
    if not quoted:
        return text
    allowed = {s.price for s in await ServiceRepository(session).list_active()}
    unknown = quoted - allowed
    if not unknown:
        return text
    logger.warning("LLM quoted prices not in DB: %s — reply replaced", sorted(unknown))
    return (
        "Точную цену на это я назвать не могу — называю только цены из прайса. "
        "Если нужной услуги в прайсе нет, напишите «администратор», и я позову мастера.\n\n"
        + await price_list_text(session)
    )


class SoloFlow:
    def __init__(self, session: AsyncSession, settings: Settings, user_id: int, has_consent: bool) -> None:
        self._session = session
        self._settings = settings
        self._user_id = user_id
        self._has_consent = has_consent
        self._bookings = BookingService(session, settings)

    async def _state(self) -> SoloClient | None:
        return await self._session.get(SoloClient, self._user_id)

    async def pending(self) -> SoloClient | None:
        state = await self._state()
        if state is not None and state.pending_service_id and state.pending_date and state.pending_time:
            return state
        return None

    async def _services(self):
        return await ServiceRepository(self._session).list_active()

    async def _services_reply(self, intro: str) -> DialogResponse:
        services = await self._services()
        text = f"{intro}\n\n{await price_list_text(self._session)}"
        if not services:
            return DialogResponse.reply(text)
        nearest = await self._bookings.upcoming_slots(
            min(s.duration_minutes for s in services), limit=4, per_day=2
        )
        if nearest:
            text += "\n\nБлижайшие свободные окна: " + ", ".join(
                f"{d.strftime('%d.%m')} {t.strftime('%H:%M')}" for d, t in nearest
            )
        text += "\n\nВыберите услугу — покажу время, на которое можно записаться прямо здесь."
        return DialogResponse.reply(
            text, keyboard_json=solo_services_keyboard([(s.id, s.name, s.price) for s in services])
        )

    async def _slots_reply(self, service_id: int, prefix: str = "") -> DialogResponse:
        service = await ServiceRepository(self._session).get_by_id(service_id)
        if service is None or not service.is_active:
            return await self._services_reply("Такой услуги нет в прайсе.")
        slots = await self._bookings.upcoming_slots(service.duration_minutes)
        if not slots:
            phone = f" Телефон: {self._settings.salon_phone}" if self._settings.salon_phone else ""
            return DialogResponse.reply(
                f"{prefix}На «{service.name}» в ближайшие две недели свободных окон нет.{phone}"
            )
        return DialogResponse.reply(
            f"{prefix}«{service.name}» — {service.price} ₽, {service.duration_minutes} мин.\n"
            "Ближайшее свободное время:",
            keyboard_json=solo_slots_keyboard(service.id, slots),
        )

    async def handle(self, message: IncomingMessage) -> DialogResponse | None:
        """Ответ режима одиночки или None, если сообщение обрабатывает обычный поток."""
        payload = message.payload or {}
        intent = message.intent
        state = await self._state()

        if intent in ("book", "services", "slots"):
            return await self._services_reply("Вот что можно записать:")

        if intent == "solo_service":
            try:
                return await self._slots_reply(int(payload.get("service_id") or 0))
            except (TypeError, ValueError):
                return await self._services_reply("Не удалось определить услугу.")

        if intent == "solo_slot":
            return await self._choose_slot(payload, state)

        pending = await self.pending()
        if pending is not None and self._has_consent:
            phone = extract_phone(message.text)
            if phone:
                return await self._book(pending, phone)

        if state is None:
            self._session.add(SoloClient(user_vk_id=self._user_id))
            await self._session.flush()
            return await self._services_reply(
                f"Здравствуйте! Это «{self._settings.salon_name}», отвечаю сразу, в любое время."
            )
        return None

    async def _choose_slot(self, payload: dict, state: SoloClient | None) -> DialogResponse:
        try:
            service_id = int(payload.get("service_id") or 0)
            day = date.fromisoformat(str(payload.get("date")))
            start = time.fromisoformat(str(payload.get("time")))
        except (TypeError, ValueError):
            return await self._services_reply("Не удалось разобрать время, выберите ещё раз.")
        service = await ServiceRepository(self._session).get_by_id(service_id)
        if service is None:
            return await self._services_reply("Такой услуги нет в прайсе.")
        if state is None:
            state = SoloClient(user_vk_id=self._user_id)
            self._session.add(state)
        state.pending_service_id, state.pending_date, state.pending_time = service_id, day, start
        await self._session.flush()
        chosen = f"Вы выбрали: {service.name}, {when(day, start)}."
        if not self._has_consent:
            url = self._settings.privacy_policy_url
            policy = f"\nПолитика обработки персональных данных: {url}" if url else ""
            return DialogResponse.reply(
                f"{chosen}\nДля записи нужен ваш телефон. Чтобы его сохранить, нужно согласие на обработку "
                f"персональных данных.{policy}\nНажмите «Согласен», затем пришлите номер телефона.",
                keyboard_json=consent_keyboard(),
            )
        return DialogResponse.reply(f"{chosen}\nПришлите номер телефона — и запись готова.")

    async def _book(self, pending: SoloClient, phone: str) -> DialogResponse:
        service = await ServiceRepository(self._session).get_by_id(pending.pending_service_id)
        day, start = pending.pending_date, pending.pending_time
        pending.pending_service_id = pending.pending_date = pending.pending_time = None
        await self._session.flush()
        if service is None:
            return await self._services_reply("Услуга больше не доступна.")
        service_id, name, price = service.id, service.name, service.price
        await self._session.commit()  # короткая транзакция записи идёт отдельно
        result = await self._bookings.create(self._user_id, name, day, start, phone)
        if not result.ok:
            if result.nearest:
                return DialogResponse.reply(
                    f"{result.error}. Ближайшее свободное время:",
                    keyboard_json=solo_slots_keyboard(service_id, result.nearest),
                )
            return await self._slots_reply(service_id, prefix=f"{result.error}.\n")
        response = DialogResponse.reply(
            f"Записала вас: {name} — {price} ₽, {when(day, start)}. "
            f"Запись #{result.booking_id} ждёт подтверждения, пришлю сообщение."
        )
        response.actions.append(
            DialogAction(type=DialogActionType.NOTIFY_BOOKING, booking_id=result.booking_id, notify_kind="new")
        )
        return response
