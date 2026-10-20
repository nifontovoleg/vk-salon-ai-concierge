"""Thin VK Long Poll handlers — delegate to services."""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Awaitable, Callable

from vkbottle.bot import Bot, Message

from app.clients.openrouter import OpenRouterClient
from app.config import Settings
from app.db.models import BookingStatus
from app.db.session import get_session
from app.handlers.converters import message_from_vk
from app.keyboards import (
    admin_notify_keyboard,
    admin_release_keyboard,
    booking_admin_keyboard,
    handoff_keyboard,
    main_keyboard,
)
from app.repositories import ProcessedMessageRepository
from app.schemas.message import DialogActionType, DialogResponse
from app.services.admin_service import AdminService
from app.services.booking_service import (
    BookingService,
    format_admin_notification,
    format_client_status,
)
from app.services.dialog_service import DialogService

logger = logging.getLogger(__name__)

BUTTON_INTENTS = frozenset(
    {
        "book", "services", "slots", "admin", "resume", "consent_yes", "consent_no",
        "my_bookings", "booking_client_cancel", "booking_reschedule", "booking_move",
        "solo_service", "solo_slot",
    }
)
ADMIN_ONLY_TEXT = "Эта кнопка доступна только администраторам."
ERROR_TEXT = "Извините, что-то пошло не так. Попробуйте ещё раз или нажмите «Администратор»."

Answer = Callable[[str, str | None], Awaitable[None]]


def _parse_payload(message: Message) -> dict:
    payload = message.payload or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    return payload if isinstance(payload, dict) else {}


def _message_key(message: Message) -> int | None:
    if message.conversation_message_id:
        return int(message.conversation_message_id)
    if message.id:
        return int(message.id)
    return None


async def _claim_message(message: Message) -> bool:
    vk_message_id = _message_key(message)
    if vk_message_id is None:
        return True

    async with get_session() as session:
        repo = ProcessedMessageRepository(session)
        claimed = await repo.try_claim(message.peer_id, vk_message_id)

    if not claimed:
        logger.info(
            "Skip duplicate VK message peer_id=%s conversation_message_id=%s",
            message.peer_id,
            vk_message_id,
        )
    return claimed


async def send(api, user_id: int, text: str, keyboard: str | None = None) -> bool:
    """Send a VK message; never raises."""
    try:
        await api.messages.send(
            user_id=user_id,
            message=text,
            keyboard=keyboard,
            random_id=random.randint(1, 2_000_000_000),
        )
        return True
    except Exception:
        logger.exception("Failed to send VK message to %s", user_id)
        return False


def register_handlers(bot: Bot, settings: Settings, openrouter: OpenRouterClient) -> None:
    @bot.on.message()
    async def message_handler(message: Message) -> None:
        if not await _claim_message(message):
            return

        async def answer(text: str, keyboard: str | None = None) -> None:
            await message.answer(text, keyboard=keyboard)

        try:
            await route_message(message, settings, openrouter, answer)
        except Exception:
            logger.exception("Unhandled error for user %s", message.from_id)
            try:
                await message.answer(ERROR_TEXT, keyboard=main_keyboard())
            except Exception:
                logger.exception("Failed to send error reply")


async def route_message(
    message: Message, settings: Settings, openrouter: OpenRouterClient, answer: Answer
) -> None:
    text = (message.text or "").strip()
    payload = _parse_payload(message)
    action = payload.get("action")
    api = message.ctx_api

    if text in ("/start", "Начать"):
        await answer(
            f"Здравствуйте! Я AI-консьерж салона «{settings.salon_name}». "
            "Помогу записаться, расскажу об услугах и ценах. Чем могу помочь?",
            main_keyboard(),
        )
        return

    if action in ("take_dialog", "release_dialog"):
        user_id = int(payload.get("user_id") or 0)
        handler = handle_take_dialog if action == "take_dialog" else handle_release_dialog
        await answer(await handler(api, message.from_id, user_id), None)
        return

    if action in ("booking_confirm", "booking_cancel"):
        status = BookingStatus.CONFIRMED if action == "booking_confirm" else BookingStatus.CANCELLED
        reply = await handle_booking_decision(
            api, settings, message.from_id, int(payload.get("booking_id") or 0), status
        )
        await answer(reply, None)
        return

    incoming = message_from_vk(message)
    incoming.intent = action if action in BUTTON_INTENTS else None
    incoming.payload = payload

    async with get_session() as session:
        response = await DialogService(session, openrouter, settings).handle_message(incoming)

    await execute_response(api, settings, incoming.user_id, response, answer)


async def execute_response(
    api, settings: Settings, user_id: int, response: DialogResponse, answer: Answer
) -> None:
    """Run DialogResponse actions in order."""
    for action in response.actions:
        if action.type == DialogActionType.REPLY:
            await answer(action.text, response.keyboard_json or main_keyboard())
        elif action.type == DialogActionType.TRANSFER_TO_ADMIN:
            await answer(action.text, response.keyboard_json or handoff_keyboard())
            await notify_admins(api, user_id, action.transfer_reason)
        elif action.type == DialogActionType.NOTIFY_BOOKING and action.booking_id:
            await notify_booking(api, action.booking_id, action.notify_kind, action.text or None)
        elif action.type == DialogActionType.FORWARD_TO_ADMIN:
            await forward_to_admin(api, settings, action.admin_vk_id, user_id, action.text)


async def notify_admins(api, user_vk_id: int, reason: str | None) -> int:
    async with get_session() as session:
        admins = await AdminService(session).list_admins_except(user_vk_id)

    if not admins:
        logger.warning("No active administrators to notify for user %s", user_vk_id)
        return 0

    text = (
        f"🔔 Запрос администратора\n"
        f"Пользователь: vk.com/id{user_vk_id}\n"
        f"Причина: {reason or 'не указана'}"
    )
    sent = 0
    for admin in admins:
        sent += await send(api, admin.vk_id, text, admin_notify_keyboard(user_vk_id))
    return sent


async def notify_booking(api, booking_id: int, kind: str = "new", note: str | None = None) -> int:
    """Send a booking card to admins. kind: new | rescheduled | cancelled."""
    async with get_session() as session:
        booking = await BookingService(session).get(booking_id)
        admins = await AdminService(session).list_admins()
        text = None
        if booking is not None:
            header = None
            if kind == "rescheduled":
                header = f"Клиент перенёс запись #{booking.id} (было: {note or '?'}), нужно подтвердить заново"
            elif kind == "cancelled":
                header = f"Клиент отменил запись #{booking.id}"
            text = format_admin_notification(booking, header)

    if text is None:
        logger.warning("Booking #%s not found for notification", booking_id)
        return 0
    if not admins:
        logger.warning("Booking #%s (%s): no active administrators to notify", booking_id, kind)
        return 0
    keyboard = None if kind == "cancelled" else booking_admin_keyboard(booking_id)
    sent = 0
    for admin in admins:
        sent += await send(api, admin.vk_id, text, keyboard)
    return sent


async def forward_to_admin(
    api, settings: Settings, admin_vk_id: int | None, user_id: int, text: str
) -> int:
    """Forward a client message to the admin who took the dialog (or all admins)."""
    if admin_vk_id:
        recipients = [admin_vk_id]
    else:
        async with get_session() as session:
            recipients = [a.vk_id for a in await AdminService(session).list_admins_except(user_id)]
    body = (
        f"Сообщение от клиента vk.com/id{user_id}:\n{text}\n\n"
        f"Ответить: https://vk.com/gim{settings.vk_group_id}?sel={user_id}"
    )
    sent = 0
    for recipient in recipients:
        sent += await send(api, recipient, body, admin_release_keyboard(user_id))
    return sent


async def handle_take_dialog(api, admin_id: int, user_id: int) -> str:
    if not user_id:
        return "Не удалось определить пользователя."

    async with get_session() as session:
        admin_service = AdminService(session)
        if not await admin_service.is_admin(admin_id):
            return ADMIN_ONLY_TEXT
        if user_id == admin_id:
            await admin_service.resolve_transfer(user_id)
            return (
                "Это ваш собственный запрос — диалог с ботом не блокируется. "
                "Продолжайте писать или используйте кнопки."
            )
        outcome = await admin_service.engage_admin(user_id, admin_id)

    if outcome == "no_transfer":
        return "Запрос уже закрыт: клиент вернулся к боту или диалог завершён."
    if outcome == "taken":
        return "Диалог уже ведёт другой администратор."
    if outcome == "already_yours":
        return f"Вы уже ведёте диалог с пользователем {user_id}."

    await send(
        api,
        user_id,
        "Администратор подключился к диалогу. Нажмите «Вернуться к боту» или напишите «отмена», "
        "чтобы снова общаться с ботом.",
        handoff_keyboard(),
    )
    return (
        f"Вы взяли диалог с пользователем {user_id}. Его сообщения будут приходить сюда, "
        "бот ему не отвечает. Нажмите «Завершить», чтобы вернуть клиента боту."
    )


async def handle_release_dialog(api, admin_id: int, user_id: int) -> str:
    if not user_id:
        return "Не удалось определить пользователя."

    async with get_session() as session:
        admin_service = AdminService(session)
        if not await admin_service.is_admin(admin_id):
            return ADMIN_ONLY_TEXT
        transfer = await admin_service.resolve_transfer(user_id)

    if transfer is None:
        return f"Активного диалога с пользователем {user_id} нет."
    if user_id != admin_id:
        await send(
            api,
            user_id,
            "Администратор завершил диалог. Я снова на связи — чем могу помочь?",
            main_keyboard(),
        )
    return f"Диалог с пользователем {user_id} возвращён боту."


async def handle_booking_decision(
    api, settings: Settings, admin_id: int, booking_id: int, status: str
) -> str:
    if not booking_id:
        return "Не удалось определить запись."

    async with get_session() as session:
        if not await AdminService(session).is_admin(admin_id):
            return ADMIN_ONLY_TEXT
        booking, outcome = await BookingService(session).set_status(booking_id, status)
        client_text = format_client_status(booking, settings) if outcome == "ok" else None
        client_id = booking.user_vk_id if booking else None
        current = booking.status if booking else None

    if outcome == "not_found":
        return f"Запись #{booking_id} не найдена."
    if outcome == "already":
        from app.services.booking_service import STATUS_LABELS

        return f"Запись #{booking_id} уже {STATUS_LABELS.get(current, current)}."

    delivered = await send(api, client_id, client_text, main_keyboard())
    verb = "подтверждена" if status == BookingStatus.CONFIRMED else "отменена"
    suffix = "клиент уведомлён" if delivered else "уведомить клиента не удалось"
    return f"Запись #{booking_id} {verb}, {suffix}."
