"""Core dialog processing service — transport-agnostic."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, time

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.openrouter import OpenRouterClient
from app.config import Settings
from app.db.models import MessageRole
from app.keyboards import (
    consent_keyboard,
    handoff_keyboard,
    my_bookings_keyboard,
    reschedule_keyboard,
)
from app.privacy import PHONE_LABEL, LlmPrivacy, extract_phone
from app.repositories import ConsentRepository, DialogRepository
from app.schemas.message import (
    AttachmentType,
    DialogAction,
    DialogActionType,
    DialogResponse,
    IncomingMessage,
)
from app.services.admin_service import AdminService
from app.services.booking_service import BookingService, describe_booking, when
from app.services.fallback_service import FallbackService
from app.services.schedule import DAY_LABELS
from app.services.solo_service import SOLO_PROMPT, SoloFlow, guard_prices
from app.services.tools import TOOL_DEFINITIONS, ToolExecutor

SOLO_TOOL_DEFINITIONS = [t for t in TOOL_DEFINITIONS if t["function"]["name"] != "get_masters"]

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 5

# Запрос администратора: только целые слова/фразы. Одно слово «человек» не считается.
ADMIN_PATTERNS = [
    re.compile(p)
    for p in (
        r"\bадмин(?:истратор)?(?:а|у|ом|е|ы|ов|ам)?\b",
        r"\bоператор(?:а|у|ом|е|ы)?\b",
        r"\bменеджер(?:а|у|ом|е|ы)?\b",
        r"\bжив(?:ой|ого|ым|ому|ая|ую)\s+(?:человек(?:а|ом|у)?|люд(?:и|ьми)|сотрудник(?:а|ом)?)\b",
        r"\b(?:позов\w*|позвать|позови\w*|нужен|нужна|соедин\w*|свяж\w*|переключ\w*)\s+"
        r"(?:с\s+|на\s+)?(?:человек(?:а|ом)?|сотрудник(?:а|ом)?)\b",
        r"\b(?:поговорить|пообщаться|связаться|соединить|соедините|свяжите|переключите)\s+"
        r"(?:меня\s+)?(?:с|на)\s+(?:живым\s+)?(?:человеком|сотрудником|человека|сотрудника)\b",
    )
]

# Возврат к боту — только если сообщение целиком является командой.
RESUME_COMMANDS = frozenset({"отмена", "назад", "вернуться к боту", "к боту", "вернуться в бот"})
CONSENT_COMMANDS = frozenset({"согласен", "согласна", "я согласен", "я согласна", "да согласен", "да согласна"})
REVOKE_COMMANDS = frozenset({"отозвать согласие", "отзываю согласие"})
GREETING_WORDS = ("привет", "здравств", "добрый", "хай", "hello")
NO_CONSENT_ALLOWED = {"greeting", "services", "slots", "masters", "address"}
CLIENT_BOOKING_INTENTS = frozenset(
    {"my_bookings", "booking_client_cancel", "booking_reschedule", "booking_move"}
)
MY_BOOKINGS_COMMANDS = frozenset({"мои записи", "моя запись", "мои брони"})


def normalize_command(text: str) -> str:
    lowered = text.lower().replace("ё", "е").strip()
    lowered = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def wants_admin(text: str) -> bool:
    lowered = text.lower().replace("ё", "е")
    return any(pattern.search(lowered) for pattern in ADMIN_PATTERNS)


def is_resume_command(text: str) -> bool:
    return normalize_command(text) in RESUME_COMMANDS


@dataclass
class _GenerationResult:
    text: str


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
        self._consents = ConsentRepository(session)
        self._admin_service = AdminService(session)

    # ------------------------------------------------------------------ entry

    async def handle_message(self, message: IncomingMessage) -> DialogResponse:
        """Process an incoming message and return response actions."""
        user_id = message.user_id
        command = normalize_command(message.text)
        transfer = await self._admin_service.get_active_transfer(user_id)

        if message.intent == "resume" or is_resume_command(message.text):
            if transfer is not None:
                return await self._resume(user_id, transfer)
            if message.intent == "resume":
                return DialogResponse.reply("Вы и так общаетесь с ботом. Чем могу помочь?")

        if transfer is not None and transfer.admin_engaged:
            # Администратор ведёт диалог: бот молчит и пересылает сообщение ему.
            return DialogResponse(
                actions=[
                    DialogAction(
                        type=DialogActionType.FORWARD_TO_ADMIN,
                        text=self._build_user_content(message),
                        admin_vk_id=transfer.admin_vk_id,
                    )
                ]
            )

        if message.intent == "consent_yes" or command in CONSENT_COMMANDS:
            await self._consents.give(user_id, self._settings.privacy_policy_url)
            if self._settings.solo_mode and await SoloFlow(
                self._session, self._settings, user_id, True
            ).pending():
                return DialogResponse.reply("Спасибо, согласие сохранено. Пришлите номер телефона — и запись готова.")
            return DialogResponse.reply(
                "Спасибо, согласие сохранено. Напишите ваш вопрос ещё раз — отвечу.\n"
                "Отозвать согласие: напишите «отозвать согласие»."
            )
        if message.intent == "consent_no":
            return DialogResponse.reply(
                "Хорошо. Без согласия работают кнопки: услуги и цены, свободное время, администратор."
            )
        if command in REVOKE_COMMANDS:
            await self._consents.revoke(user_id)
            await self._dialogs.delete_history(user_id)
            return DialogResponse.reply(
                "Согласие отозвано, история переписки с ботом удалена. "
                "Кнопки продолжают работать."
            )

        if message.intent in CLIENT_BOOKING_INTENTS or command in MY_BOOKINGS_COMMANDS:
            return await self._client_booking_action(
                user_id, message.intent or "my_bookings", message.payload or {}
            )

        if message.intent == "admin" or wants_admin(message.text):
            return await self._request_admin(user_id, "Пользователь запросил администратора", transfer)

        fallback = FallbackService(self._session, self._settings, user_id)
        has_consent = await self._has_consent(user_id)

        if self._settings.solo_mode:
            solo = await SoloFlow(self._session, self._settings, user_id, has_consent).handle(message)
            if solo is not None:
                return solo

        if message.intent in ("book", "services", "slots"):
            reply = await fallback.reply_by_intent(message.intent)
            if message.intent == "book" and not has_consent:
                return DialogResponse.reply(
                    f"{reply}\n\n{self._consent_text()}", keyboard_json=consent_keyboard()
                )
            return DialogResponse.reply(reply)

        user_content = self._build_user_content(message)

        if self._is_short_greeting(user_content):
            greeting = await fallback.try_reply(user_content)
            if greeting:
                return DialogResponse.reply(greeting)

        if not has_consent and fallback.classify(user_content) == "my_bookings":
            return await self._client_booking_action(user_id, "my_bookings", {})

        if not has_consent:
            keyword_reply = await fallback.try_reply(user_content, allowed=NO_CONSENT_ALLOWED)
            if keyword_reply:
                return DialogResponse.reply(keyword_reply)
            return DialogResponse.reply(self._consent_text(), keyboard_json=consent_keyboard())

        await self._dialogs.add_message(user_id, MessageRole.USER, user_content)
        # Коммит до запроса к модели: SQLite не держит блокировку записи, пока ждём LLM.
        await self._session.commit()
        return await self._llm_reply(user_id, user_content, fallback)

    # ---------------------------------------------------------------- helpers

    async def _client_booking_action(self, user_id: int, intent: str, payload: dict) -> DialogResponse:
        """«Мои записи», отмена и перенос своей записи кнопками — без LLM."""
        service = BookingService(self._session, self._settings)
        cutoff = self._settings.cancel_min_hours

        if intent == "my_bookings":
            bookings = await service.list_upcoming(user_id)
            if not bookings:
                return DialogResponse.reply(
                    "У вас нет предстоящих записей. Нажмите «Записаться», чтобы выбрать время."
                )
            lines = ["Ваши записи:"] + [describe_booking(b) for b in bookings[:5]]
            if len(bookings) > 5:
                lines.append(f"…и ещё {len(bookings) - 5}, показаны ближайшие 5.")
            if cutoff:
                lines.append(f"\nОтменить или перенести онлайн можно не позднее чем за {cutoff:g} ч до начала.")
            return DialogResponse.reply(
                "\n".join(lines), keyboard_json=my_bookings_keyboard([b.id for b in bookings[:5]])
            )

        try:
            booking_id = int(payload.get("booking_id") or 0)
        except (TypeError, ValueError):
            booking_id = 0
        if not booking_id:
            return DialogResponse.reply("Не удалось определить запись. Нажмите «Мои записи».")

        if intent == "booking_client_cancel":
            result = await service.cancel_by_client(user_id, booking_id)
            if not result.ok:
                return DialogResponse.reply(result.error)
            response = DialogResponse.reply(
                f"Запись #{booking_id} на {result.old_when} отменена. Администратор получит уведомление."
            )
            response.actions.append(
                DialogAction(type=DialogActionType.NOTIFY_BOOKING, booking_id=booking_id, notify_kind="cancelled")
            )
            return response

        if intent == "booking_reschedule":
            error = await service.check_changeable(user_id, booking_id)
            if error:
                return DialogResponse.reply(error)
            booking = await service.get(booking_id)
            slots = await service.upcoming_slots(
                booking.service.duration_minutes, booking.master_id, exclude_booking_id=booking_id
            )
            if not slots:
                phone = f" Позвоните в салон: {self._settings.salon_phone}" if self._settings.salon_phone else ""
                return DialogResponse.reply(f"Свободного времени в ближайшие две недели нет.{phone}")
            return DialogResponse.reply(
                f"Выберите новое время для записи {describe_booking(booking)}:",
                keyboard_json=reschedule_keyboard(booking_id, slots),
            )

        # booking_move
        try:
            day = date.fromisoformat(str(payload.get("date")))
            start = time.fromisoformat(str(payload.get("time")))
        except ValueError:
            return DialogResponse.reply("Не удалось разобрать время. Нажмите «Мои записи» ещё раз.")
        result = await service.reschedule(user_id, booking_id, day, start)
        if not result.ok:
            if result.nearest:
                return DialogResponse.reply(
                    f"{result.error}. Ближайшее свободное время:",
                    keyboard_json=reschedule_keyboard(booking_id, result.nearest),
                )
            return DialogResponse.reply(result.error)
        response = DialogResponse.reply(
            f"Запись #{booking_id} перенесена на {when(day, start)}. "
            "Она снова ждёт подтверждения администратора."
        )
        response.actions.append(
            DialogAction(
                type=DialogActionType.NOTIFY_BOOKING,
                booking_id=booking_id,
                notify_kind="rescheduled",
                text=result.old_when or "",
            )
        )
        return response

    async def _has_consent(self, user_id: int) -> bool:
        if not self._settings.require_consent:
            return True
        return await self._consents.has(user_id)

    def _consent_text(self) -> str:
        url = self._settings.privacy_policy_url
        policy = f"Политика обработки персональных данных: {url}" if url else (
            "Политику обработки персональных данных можно запросить у администратора."
        )
        return (
            "Чтобы ответить на сообщение в свободной форме и записать вас через чат, "
            "текст переписки передаётся AI-сервису (OpenRouter, серверы за пределами РФ). "
            "Перед отправкой в ИИ-модель персональные данные заменяются обезличенными метками, "
            "переписка хранится в базе салона.\n"
            f"{policy}\n\n"
            "Нажмите «Согласен» или напишите «согласен». Без согласия работают кнопки "
            "«Услуги и цены», «Свободное время» и «Администратор»."
        )

    def _no_admins_text(self) -> str:
        s = self._settings
        lines = ["Сейчас администраторы в чате не подключены, поэтому передать диалог я не могу."]
        contacts = []
        if s.salon_phone:
            contacts.append(f"Телефон: {s.salon_phone}")
        if s.salon_address:
            contacts.append(f"Адрес: {s.salon_address}")
        if s.salon_working_hours:
            contacts.append(f"Часы работы: {s.salon_working_hours}")
        if contacts:
            lines.append("Свяжитесь с салоном напрямую:")
            lines.extend(contacts)
        lines.append("А с услугами, ценами и записью могу помочь я.")
        return "\n".join(lines)

    async def _request_admin(self, user_id: int, reason: str, transfer) -> DialogResponse:
        admins = await self._admin_service.list_admins_except(user_id)
        if not admins:
            logger.warning("Handoff requested by user %s, but no active administrators", user_id)
            return DialogResponse.reply(self._no_admins_text())
        if transfer is not None:
            return DialogResponse.reply(
                "Запрос уже передан администратору, он скоро подключится. "
                "Нажмите «Вернуться к боту» или напишите «отмена», чтобы снять запрос.",
                keyboard_json=handoff_keyboard(),
            )
        await self._admin_service.create_transfer(user_id, reason)
        return DialogResponse.transfer_to_admin(
            "Передал запрос администратору. Пока ждёте — могу ответить на вопросы об услугах и записи.\n"
            "Нажмите «Вернуться к боту» или напишите «отмена», чтобы снять запрос.",
            reason=reason,
            keyboard_json=handoff_keyboard(),
        )

    async def _resume(self, user_id: int, transfer) -> DialogResponse:
        engaged_admin = transfer.admin_vk_id if transfer.admin_engaged else None
        await self._admin_service.resolve_transfer(user_id)
        actions = [
            DialogAction(
                type=DialogActionType.REPLY,
                text="Хорошо, снова на связи бот. Могу рассказать об услугах или помочь записаться.",
            )
        ]
        if engaged_admin:
            actions.append(
                DialogAction(
                    type=DialogActionType.FORWARD_TO_ADMIN,
                    text="Клиент вернулся к боту, диалог закрыт.",
                    admin_vk_id=engaged_admin,
                )
            )
        return DialogResponse(actions=actions)

    async def _llm_reply(
        self, user_id: int, user_content: str, fallback: FallbackService
    ) -> DialogResponse:
        history = await self._dialogs.get_recent_messages(
            user_id, limit=self._settings.dialog_history_limit
        )
        known_phone = extract_phone(user_content)
        if known_phone is None:
            for msg in reversed(history):
                if msg.role == MessageRole.USER:
                    known_phone = extract_phone(msg.content)
                    if known_phone:
                        break

        executor = ToolExecutor(
            self._session, user_id, settings=self._settings, known_phone=known_phone
        )
        try:
            privacy = LlmPrivacy.from_settings(self._settings, known_phone)
            result = await self._generate_reply(history, user_content, executor, privacy)
            text = result.text
        except Exception as exc:  # LLM недоступен, битый ответ, ошибка инструмента
            logger.error("LLM dialog failed for user %s: %s", user_id, exc, exc_info=True)
            text = await self._safe_fallback(fallback, user_content)
            if executor.created_booking_ids:
                ids = ", ".join(f"#{i}" for i in executor.created_booking_ids)
                text = f"Запись {ids} создана и ждёт подтверждения администратора.\n\n{text}"

        if self._settings.solo_mode:
            text = await guard_prices(self._session, text)

        if executor.transfer_unavailable and not executor.transfer_requested:
            text = self._no_admins_text()

        await self._dialogs.add_message(user_id, MessageRole.ASSISTANT, text)

        if executor.transfer_requested:
            response = DialogResponse.transfer_to_admin(
                text, reason=executor.transfer_reason, keyboard_json=handoff_keyboard()
            )
        else:
            response = DialogResponse.reply(text)
        for booking_id, kind, note in executor.notifications:
            response.actions.append(
                DialogAction(
                    type=DialogActionType.NOTIFY_BOOKING,
                    booking_id=booking_id,
                    notify_kind=kind,
                    text=note or "",
                )
            )
        return response

    async def _safe_fallback(self, fallback: FallbackService, user_content: str) -> str:
        try:
            backup = await fallback.try_reply(user_content)
        except Exception:
            logger.exception("Fallback reply failed")
            backup = None
        return backup or (
            "Сейчас AI временно недоступен. Используйте кнопки:\n"
            "«Записаться», «Услуги и цены», «Свободное время», «Администратор»."
        )

    async def _generate_reply(
        self,
        history: list,
        user_content: str,
        executor: ToolExecutor,
        privacy: LlmPrivacy | None = None,
    ) -> _GenerationResult:
        """Call OpenRouter with history and tools, return final text.

        Всё, что уходит в модель (история, текст клиента, результаты инструментов),
        проходит через privacy.mask; аргументы инструментов и итоговый текст —
        через privacy.restore, поэтому в базу и клиенту попадают настоящие данные.
        """
        privacy = privacy or LlmPrivacy.from_settings(self._settings)
        messages: list[dict] = [{"role": "system", "content": self._load_system_prompt(executor)}]
        for msg in history[:-1]:
            if msg.role == MessageRole.USER:
                messages.append({"role": "user", "content": privacy.mask(msg.content)})
            elif msg.role == MessageRole.ASSISTANT:
                content = privacy.mask(msg.content) if privacy.enabled else msg.content
                messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": privacy.mask(user_content)})

        for _ in range(MAX_TOOL_ITERATIONS):
            tools = SOLO_TOOL_DEFINITIONS if self._settings.solo_mode else TOOL_DEFINITIONS
            response = await self._openrouter.chat_completion(messages, tools=tools)
            assistant_msg = self._openrouter.extract_assistant_message(response)

            tool_calls = assistant_msg.get("tool_calls")
            if not tool_calls:
                content = privacy.restore(assistant_msg.get("content") or "")
                return _GenerationResult(text=content.strip() or "Извините, не удалось сформировать ответ.")

            messages.append(assistant_msg)
            for tool_call in tool_calls:
                fn = tool_call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Invalid tool arguments JSON for %s", fn.get("name"))
                    result = json.dumps(
                        {"error": "Аргументы инструмента не являются корректным JSON, повтори вызов"},
                        ensure_ascii=False,
                    )
                else:
                    # Метки в аргументах ([ТЕЛЕФОН_1], [ИМЯ_1]) → настоящие данные до записи в базу.
                    result = await executor.execute(str(fn.get("name")), privacy.restore(args))
                messages.append(
                    {"role": "tool", "tool_call_id": tool_call.get("id", ""), "content": privacy.mask(result)}
                )

            if executor.transfer_requested or executor.transfer_unavailable:
                final = await self._openrouter.chat_completion(messages)
                assistant_msg = self._openrouter.extract_assistant_message(final)
                text = privacy.restore(assistant_msg.get("content") or "Передаю ваш запрос администратору.").strip()
                return _GenerationResult(text=text)

        return _GenerationResult(
            text="Извините, запрос слишком сложный. Напишите «администратор» или используйте кнопки.",
        )

    def _load_system_prompt(self, executor: ToolExecutor) -> str:
        path = self._settings.prompts_dir / "system_prompt.txt"
        template = path.read_text(encoding="utf-8")
        now = executor.schedule.now()
        unknown = "уточняйте у администратора"
        prompt = template.format(
            salon_name=self._settings.salon_name,
            salon_address=self._settings.salon_address or unknown,
            salon_phone=self._settings.salon_phone or unknown,
            salon_working_hours=self._settings.salon_working_hours or "10:00-20:00 ежедневно",
            today=now.date().isoformat(),
            weekday=DAY_LABELS[now.weekday()],
            now_time=now.strftime("%H:%M"),
            timezone=self._settings.timezone,
            phone_label=PHONE_LABEL,
        )
        return prompt + SOLO_PROMPT if self._settings.solo_mode else prompt

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
        return wants_admin(text)

    @staticmethod
    def _wants_resume(text: str) -> bool:
        return is_resume_command(text)

    @staticmethod
    def _is_short_greeting(text: str) -> bool:
        lowered = text.lower().strip()
        return len(lowered) < 40 and any(word in lowered for word in GREETING_WORDS)
