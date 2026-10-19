"""Fix 1: admins get notified about bookings and confirm/cancel them."""

import json
from unittest.mock import AsyncMock

from app.db.models import BookingStatus
from app.handlers.messages import (
    ADMIN_ONLY_TEXT,
    execute_response,
    handle_booking_decision,
    notify_booking,
)
from app.schemas.message import DialogActionType, IncomingMessage
from app.services.dialog_service import DialogService
from app.services.tools import ToolExecutor
from tests.helpers import ADMIN_ID, CLIENT_ID, future_day, make_settings

DAY = str(future_day(2))


async def _book(factory, settings, **overrides) -> int:
    args = {"service_name": "Женская стрижка", "date": DAY, "time": "12:00", "phone": "+79991234567",
            "client_name": "Ольга", **overrides}
    async with factory() as session:
        executor = ToolExecutor(session, CLIENT_ID, settings=settings)
        result = json.loads(await executor.execute("create_booking", args))
        await session.commit()
    assert result["success"], result
    return result["booking_id"]


async def test_admin_notified_with_details(app_db, fake_api):
    settings = make_settings()
    booking_id = await _book(app_db, settings, master_name="Анна")
    assert await notify_booking(fake_api, booking_id) == 1

    [text] = fake_api.to(ADMIN_ID)
    assert f"#{booking_id}" in text
    assert "Женская стрижка" in text and "Анна" in text
    assert "12:00" in text and "Ольга" in text and "+79991234567" in text
    keyboard = json.loads(fake_api.sent[0]["keyboard"])
    actions = [b["action"]["payload"]["action"] for b in keyboard["buttons"][0]]
    assert actions == ["booking_confirm", "booking_cancel"]


async def test_confirm_notifies_client_once(app_db, fake_api):
    settings = make_settings()
    booking_id = await _book(app_db, settings)

    reply = await handle_booking_decision(fake_api, settings, ADMIN_ID, booking_id, BookingStatus.CONFIRMED)
    assert "подтверждена" in reply and "клиент уведомлён" in reply
    [client_text] = fake_api.to(CLIENT_ID)
    assert "подтверждена" in client_text

    again = await handle_booking_decision(fake_api, settings, ADMIN_ID, booking_id, BookingStatus.CANCELLED)
    assert "уже подтверждена" in again
    assert len(fake_api.to(CLIENT_ID)) == 1


async def test_cancel_frees_slot(app_db, fake_api):
    settings = make_settings()
    booking_id = await _book(app_db, settings, master_name="Анна")
    await handle_booking_decision(fake_api, settings, ADMIN_ID, booking_id, BookingStatus.CANCELLED)
    assert "отменил" in fake_api.to(CLIENT_ID)[0]
    # слот снова свободен
    await _book(app_db, settings, master_name="Анна")


async def test_non_admin_cannot_confirm(app_db, fake_api):
    settings = make_settings()
    booking_id = await _book(app_db, settings)
    reply = await handle_booking_decision(fake_api, settings, CLIENT_ID, booking_id, BookingStatus.CONFIRMED)
    assert reply == ADMIN_ONLY_TEXT
    assert fake_api.sent == []


async def test_llm_booking_triggers_notification(app_db, fake_api):
    settings = make_settings()
    openrouter = AsyncMock()
    openrouter.extract_assistant_message = lambda r: r["choices"][0]["message"]
    call = {"id": "c1", "type": "function", "function": {"name": "create_booking", "arguments": json.dumps(
        {"service_name": "Мужская стрижка", "date": DAY, "time": "15:00", "phone": "[телефон]"})}}
    openrouter.chat_completion.side_effect = [
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "Записал, ждите подтверждения."}}]},
    ]
    async with app_db() as session:
        response = await DialogService(session, openrouter, settings).handle_message(
            IncomingMessage(user_id=CLIENT_ID, peer_id=CLIENT_ID, text="Мужская стрижка в 15:00, 89991234567")
        )
        await session.commit()
    assert [a.type for a in response.actions] == [DialogActionType.REPLY, DialogActionType.NOTIFY_BOOKING]

    answers = []

    async def answer(text, keyboard=None):
        answers.append(text)

    await execute_response(fake_api, settings, CLIENT_ID, response, answer)
    assert answers == ["Записал, ждите подтверждения."]
    [admin_text] = fake_api.to(ADMIN_ID)
    assert "+79991234567" in admin_text  # настоящий номер, хотя модель видела только метку
