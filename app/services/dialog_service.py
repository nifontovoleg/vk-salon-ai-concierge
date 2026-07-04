"""Core dialog processing service — transport-agnostic."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.openrouter import OpenRouterClient, OpenRouterError
from app.config import Settings
from app.db.models import MessageRole
from app.repositories import DialogRepository
from app.schemas.message import AttachmentType, DialogResponse, IncomingMessage
from app.services.admin_service import AdminService
from app.services.fallback_service import FallbackService
from app.services.tools import TOOL_DEFINITIONS, ToolExecutor

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 5

ADMIN_KEYWORDS = (
    "администратор",
    "админ",
    "оператор",
    "человек",
    "менеджер",
    "живой",
)

RESUME_KEYWORDS = (
    "отмена",
    "назад",
    "продолжить",
    "вернуться",
    "к боту",
    "завершить",
)


@dataclass
class _GenerationResult:
    text: str
    transfer_requested: bool = False
    transfer_reason: str | None = None


class DialogService:
    """
    Process incoming messages and produce responses/actions.

    This service is independent of VK Long Poll or Callback API.
    Transport layers convert platform events to IncomingMessage
    and execute returned DialogResponse actions.
    """

    def __init__(
        self,
        session: AsyncSession,
        openrouter: OpenRouterClient,
        settings: Settings,
    ) -> None:
        self._session = session
        self._openrouter = openrouter
        self._settings = settings
        self._dialogs = DialogRepository(session)
        self._admin_service = AdminService(session)

    async def handle_message(self, message: IncomingMessage) -> DialogResponse:
        """Process an incoming message and return response actions."""
        fallback = FallbackService(self._session, self._settings, message.user_id)

        if self._wants_resume(message.text):
            await self._admin_service.resolve_transfer(message.user_id)
            return DialogResponse.reply(
                "Хорошо, продолжаем. Чем могу помочь? Могу рассказать об услугах или помочь записаться."
            )

        if await self._admin_service.is_admin_engaged(message.user_id):
            return DialogResponse.reply(
                "С вами на связи администратор. Напишите «отмена», чтобы вернуться к общению с ботом."
            )

        if message.intent == "admin" or self._wants_admin(message.text):
            await self._admin_service.create_transfer(message.user_id, "Пользователь запросил администратора")
            return DialogResponse.transfer_to_admin(
                "Передал запрос администратору. Пока ждёте — могу ответить на вопросы об услугах и записи.\n"
                "Напишите «отмена», чтобы снять запрос.",
                reason="Пользователь запросил администратора",
            )

        user_content = self._build_user_content(message)
        await self._dialogs.add_message(message.user_id, MessageRole.USER, user_content)

        if message.intent in ("book", "services", "slots"):
            reply = await fallback.reply_by_intent(message.intent)
            await self._dialogs.add_message(message.user_id, MessageRole.ASSISTANT, reply)
            return DialogResponse.reply(reply)

        greeting = await fallback.try_reply(user_content)
        if greeting and self._is_short_greeting(user_content):
            await self._dialogs.add_message(message.user_id, MessageRole.ASSISTANT, greeting)
            return DialogResponse.reply(greeting)

        try:
            result = await self._generate_reply(message.user_id, user_content)
        except OpenRouterError as exc:
            logger.error("OpenRouter failed for user %s: %s", message.user_id, exc)
            backup = await fallback.try_reply(user_content)
            if backup:
                await self._dialogs.add_message(message.user_id, MessageRole.ASSISTANT, backup)
                return DialogResponse.reply(backup)
            return DialogResponse.reply(
                "Сейчас AI временно недоступен. Используйте кнопки:\n"
                "«Записаться», «Услуги и цены», «Свободное время»."
            )

        await self._dialogs.add_message(message.user_id, MessageRole.ASSISTANT, result.text)

        if result.transfer_requested:
            return DialogResponse.transfer_to_admin(result.text, reason=result.transfer_reason)

        return DialogResponse.reply(result.text)

    async def _generate_reply(self, user_vk_id: int, user_content: str) -> _GenerationResult:
        """Call OpenRouter with history, tools and return final text."""
        system_prompt = self._load_system_prompt()
        history = await self._dialogs.get_recent_messages(
            user_vk_id, limit=self._settings.dialog_history_limit
        )

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for msg in history[:-1]:
            if msg.role in (MessageRole.USER, MessageRole.ASSISTANT):
                messages.append({"role": msg.role, "content": msg.content})
        messages.append({"role": "user", "content": user_content})

        executor = ToolExecutor(self._session, user_vk_id)

        for _ in range(MAX_TOOL_ITERATIONS):
            response = await self._openrouter.chat_completion(messages, tools=TOOL_DEFINITIONS)
            assistant_msg = self._openrouter.extract_assistant_message(response)

            tool_calls = assistant_msg.get("tool_calls")
            if not tool_calls:
                content = assistant_msg.get("content") or ""
                return _GenerationResult(
                    text=content.strip() or "Извините, не удалось сформировать ответ.",
                    transfer_requested=executor.transfer_requested,
                    transfer_reason=executor.transfer_reason,
                )

            messages.append(assistant_msg)
            for tool_call in tool_calls:
                fn = tool_call["function"]
                args = json.loads(fn.get("arguments") or "{}")
                result = await executor.execute(fn["name"], args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": result,
                    }
                )

            if executor.transfer_requested:
                final = await self._openrouter.chat_completion(messages)
                assistant_msg = self._openrouter.extract_assistant_message(final)
                text = (assistant_msg.get("content") or "Передаю ваш запрос администратору.").strip()
                return _GenerationResult(
                    text=text,
                    transfer_requested=True,
                    transfer_reason=executor.transfer_reason,
                )

        return _GenerationResult(
            text="Извините, запрос слишком сложный. Напишите «администратор» или используйте кнопки.",
        )

    def _load_system_prompt(self) -> str:
        path = self._settings.prompts_dir / "system_prompt.txt"
        template = path.read_text(encoding="utf-8")
        return template.format(
            salon_name=self._settings.salon_name,
            salon_address=self._settings.salon_address or "уточняйте у администратора",
            salon_phone=self._settings.salon_phone or "уточняйте у администратора",
            salon_working_hours=self._settings.salon_working_hours or "уточняйте у администратора",
        )

    @staticmethod
    def _build_user_content(message: IncomingMessage) -> str:
        parts = [message.text.strip()] if message.text.strip() else []
        for att in message.attachments:
            if att.type == AttachmentType.PHOTO:
                parts.append("[Клиент прислал фото]")
            elif att.description:
                parts.append(f"[Вложение: {att.description}]")
        return "\n".join(parts) or "[Пустое сообщение]"

    @staticmethod
    def _wants_admin(text: str) -> bool:
        lowered = text.lower().strip()
        if lowered in ADMIN_KEYWORDS:
            return True
        return any(keyword in lowered for keyword in ADMIN_KEYWORDS)

    @staticmethod
    def _wants_resume(text: str) -> bool:
        lowered = text.lower().strip()
        return any(keyword in lowered for keyword in RESUME_KEYWORDS)

    @staticmethod
    def _is_short_greeting(text: str) -> bool:
        lowered = text.lower().strip()
        return len(lowered) < 40 and any(
            word in lowered for word in ("привет", "здравств", "добрый", "хай", "hello")
        )
