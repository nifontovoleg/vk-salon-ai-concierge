"""Thin VK Long Poll handlers — delegate to DialogService."""

from __future__ import annotations

import json
import logging
import random

from vkbottle.bot import Bot, Message

from app.clients.openrouter import OpenRouterClient
from app.config import Settings
from app.db.session import get_session
from app.handlers.converters import message_from_vk
from app.keyboards import admin_notify_keyboard, main_keyboard
from app.repositories import ProcessedMessageRepository
from app.schemas.message import DialogActionType
from app.services.admin_service import AdminService
from app.services.dialog_service import DialogService

logger = logging.getLogger(__name__)

BUTTON_INTENTS = frozenset({"book", "services", "slots", "admin"})


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


def register_handlers(bot: Bot, settings: Settings, openrouter: OpenRouterClient) -> None:
    @bot.on.message()
    async def message_handler(message: Message) -> None:
        if not await _claim_message(message):
            return

        text = (message.text or "").strip()
        payload = _parse_payload(message)
        action = payload.get("action")

        if text in ("/start", "Начать"):
            await message.answer(
                f"Здравствуйте! Я AI-консьерж салона «{settings.salon_name}». "
                "Помогу записаться, расскажу об услугах и ценах. Чем могу помочь?",
                keyboard=main_keyboard(),
            )
            return

        if action == "take_dialog":
            await handle_take_dialog(message, bot)
            return

        if action == "release_dialog":
            await handle_release_dialog(message, bot)
            return

        intent = action if action in BUTTON_INTENTS else None
        await process_message(message, settings, openrouter, intent=intent)


async def handle_take_dialog(message: Message, bot: Bot) -> None:
    payload = _parse_payload(message)
    user_id = int(payload.get("user_id") or 0)
    if not user_id:
        await message.answer("Не удалось определить пользователя.")
        return

    async with get_session() as session:
        admin_service = AdminService(session)
        if not await admin_service.is_admin(message.from_id):
            await message.answer("Эта кнопка доступна только администраторам.")
            return

        if user_id == message.from_id:
            await admin_service.resolve_transfer(user_id)
            await message.answer(
                "Это ваш собственный запрос — диалог с ботом не блокируется. "
                "Продолжайте писать или используйте кнопки."
            )
            return

        await admin_service.engage_admin(user_id)

    await message.answer(f"Вы взяли диалог с пользователем {user_id}.")
    await bot.api.messages.send(
        user_id=user_id,
        message="Администратор подключился к диалогу. Напишите «отмена», чтобы вернуться к боту.",
        random_id=random.randint(1, 2_000_000_000),
    )


async def handle_release_dialog(message: Message, bot: Bot) -> None:
    payload = _parse_payload(message)
    user_id = int(payload.get("user_id") or 0)
    if not user_id:
        await message.answer("Не удалось определить пользователя.")
        return

    async with get_session() as session:
        admin_service = AdminService(session)
        if not await admin_service.is_admin(message.from_id):
            await message.answer("Эта кнопка доступна только администраторам.")
            return
        await admin_service.resolve_transfer(user_id)

    await message.answer(f"Диалог с пользователем {user_id} возвращён боту.")
    if user_id != message.from_id:
        await bot.api.messages.send(
            user_id=user_id,
            message="Администратор завершил диалог. Я снова на связи — чем могу помочь?",
            keyboard=main_keyboard(),
            random_id=random.randint(1, 2_000_000_000),
        )


async def process_message(
    message: Message,
    settings: Settings,
    openrouter: OpenRouterClient,
    intent: str | None = None,
) -> None:
    incoming = message_from_vk(message)
    incoming.intent = intent

    async with get_session() as session:
        dialog_service = DialogService(session, openrouter, settings)
        response = await dialog_service.handle_message(incoming)

    for action in response.actions:
        if action.type == DialogActionType.REPLY:
            await message.answer(
                action.text,
                keyboard=response.keyboard_json or main_keyboard(),
            )
            return

        if action.type == DialogActionType.TRANSFER_TO_ADMIN:
            await message.answer(action.text, keyboard=main_keyboard())
            await notify_admins(message, incoming.user_id, action.transfer_reason)
            return


async def notify_admins(
    message: Message,
    user_vk_id: int,
    reason: str | None,
) -> None:
    async with get_session() as session:
        admin_service = AdminService(session)
        admins = await admin_service.list_admins()

    if not admins:
        logger.warning("No active administrators to notify for user %s", user_vk_id)
        return

    text = (
        f"🔔 Запрос администратора\n"
        f"Пользователь: vk.com/id{user_vk_id}\n"
        f"Причина: {reason or 'не указана'}"
    )
    for admin in admins:
        if admin.vk_id == user_vk_id:
            continue
        try:
            await message.ctx_api.messages.send(
                user_id=admin.vk_id,
                message=text,
                keyboard=admin_notify_keyboard(user_vk_id),
                random_id=random.randint(1, 2_000_000_000),
            )
        except Exception:
            logger.exception("Failed to notify admin %s", admin.vk_id)
