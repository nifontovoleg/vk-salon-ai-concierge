"""Gap 2: client cancels/reschedules own bookings (buttons and LLM tools)."""

import json
from datetime import time
from unittest.mock import AsyncMock

from app.clients.openrouter import OpenRouterClient
from app.db.models import BookingStatus
from app.handlers.messages import execute_response, handle_booking_decision
from app.repositories import BookingRepository, ServiceRepository
from app.schemas.message import DialogActionType, IncomingMessage
from app.services.dialog_service import DialogService
from app.services.tools import ToolExecutor
from tests.helpers import ADMIN_ID, CLIENT_ID, future_day, make_settings

DAY = future_day(3)
OTHER = 99


async def _book(session, settings, user=CLIENT_ID, time_="12:00", master="Анна"):
    result = json.loads(await ToolExecutor(session, user, settings=settings).execute("create_booking", {
        "service_name": "Женская стрижка", "date": DAY.isoformat(), "time": time_,
        "phone": "+79991234567", "master_name": master}))
    assert result["success"], result
    return result["booking_id"]


async def _press(session, settings, intent, user=CLIENT_ID, **payload):
    return await DialogService(session, AsyncMock(), settings).handle_message(
        IncomingMessage(user_id=user, peer_id=user, text="", intent=intent, payload=payload)
    )


async def _answer_sink(api, settings, response):
    answers = []

    async def answer(text, keyboard=None):
        answers.append((text, keyboard))

    await execute_response(api, settings, CLIENT_ID, response, answer)
    return answers


async def test_my_bookings_lists_only_own(full_session):
    settings = make_settings()
    mine = await _book(full_session, settings)
    await _book(full_session, settings, user=OTHER, time_="14:00")
    response = await _press(full_session, settings, "my_bookings")
    [text] = response.client_texts
    assert f"#{mine}" in text and "14:00" not in text
    keyboard = json.loads(response.keyboard_json)
    payloads = [b["action"]["payload"] for row in keyboard["buttons"] for b in row]
    assert {p["action"] for p in payloads} == {"booking_client_cancel", "booking_reschedule"}
    assert all(p["booking_id"] == mine for p in payloads)


async def test_my_bookings_text_command_without_consent(full_session):
    settings = make_settings()
    booking_id = await _book(full_session, settings, user=OTHER)
    response = await DialogService(full_session, AsyncMock(), settings).handle_message(
        IncomingMessage(user_id=OTHER, peer_id=OTHER, text="Мои записи")
    )
    assert f"#{booking_id}" in response.client_texts[0]


async def test_client_cancel_notifies_admin(app_db, fake_api):
    settings = make_settings()
    async with app_db() as session:
        booking_id = await _book(session, settings)
        response = await _press(session, settings, "booking_client_cancel", booking_id=booking_id)
        await session.commit()
    assert "отменена" in response.client_texts[0]
    assert response.actions[1].notify_kind == "cancelled"
    await _answer_sink(fake_api, settings, response)
    [card] = fake_api.sent
    assert card["user_id"] == ADMIN_ID and "Клиент отменил запись" in card["message"]
    assert card["keyboard"] is None
    async with app_db() as session:
        booking = await BookingRepository(session).get_with_relations(booking_id)
        assert booking.status == BookingStatus.CANCELLED


async def test_cannot_touch_foreign_booking(full_session):
    settings = make_settings()
    foreign = await _book(full_session, settings, user=OTHER)
    response = await _press(full_session, settings, "booking_client_cancel", booking_id=foreign)
    assert "не найдена" in response.client_texts[0]
    response = await _press(full_session, settings, "booking_move", booking_id=foreign,
                            date=DAY.isoformat(), time="15:00")
    assert "не найдена" in response.client_texts[0]
    tool = json.loads(await ToolExecutor(full_session, CLIENT_ID, settings=settings).execute(
        "cancel_booking", {"booking_id": foreign}))
    assert tool["success"] is False
    booking = await BookingRepository(full_session).get_with_relations(foreign)
    assert booking.status == BookingStatus.PENDING


async def test_cutoff_blocks_online_cancel(full_session):
    settings = make_settings(cancel_min_hours=1000)
    booking_id = await _book(full_session, settings)
    response = await _press(full_session, settings, "booking_client_cancel", booking_id=booking_id)
    text = response.client_texts[0]
    assert "меньше 1000 ч" in text and settings.salon_phone in text
    response = await _press(full_session, settings, "booking_reschedule", booking_id=booking_id)
    assert "меньше 1000 ч" in response.client_texts[0]


async def test_past_booking_not_changeable(full_session):
    services = await ServiceRepository(full_session).list_active()
    booking = await BookingRepository(full_session).create(
        user_vk_id=CLIENT_ID, service_id=services[0].id, booking_date=future_day(-2),
        booking_time=time(12), client_phone="x")
    await full_session.commit()
    response = await _press(full_session, make_settings(), "booking_client_cancel", booking_id=booking.id)
    assert "уже прошла" in response.client_texts[0]


async def test_reschedule_buttons_flow(app_db, fake_api):
    settings = make_settings()
    async with app_db() as session:
        booking_id = await _book(session, settings)
        await session.commit()
    await handle_booking_decision(fake_api, settings, ADMIN_ID, booking_id, BookingStatus.CONFIRMED)
    fake_api.sent.clear()

    async with app_db() as session:
        response = await _press(session, settings, "booking_reschedule", booking_id=booking_id)
    keyboard = json.loads(response.keyboard_json)
    choices = [b["action"]["payload"] for row in keyboard["buttons"] for b in row]
    assert choices and all(c["action"] == "booking_move" and c["booking_id"] == booking_id for c in choices)

    async with app_db() as session:
        response = await _press(session, settings, "booking_move", booking_id=booking_id,
                                date=DAY.isoformat(), time="16:00")
        await session.commit()
    assert "перенесена" in response.client_texts[0]
    assert response.actions[1].notify_kind == "rescheduled"
    await _answer_sink(fake_api, settings, response)
    [card] = fake_api.to(ADMIN_ID)
    assert "перенёс" in card and "12:00" in card and "16:00" in card
    assert "booking_confirm" in fake_api.sent[0]["keyboard"]

    async with app_db() as session:
        booking = await BookingRepository(session).get_with_relations(booking_id)
        assert booking.booking_time == time(16) and booking.status == BookingStatus.PENDING
    # подтверждение снова доступно
    reply = await handle_booking_decision(fake_api, settings, ADMIN_ID, booking_id, BookingStatus.CONFIRMED)
    assert "подтверждена" in reply


async def test_reschedule_into_busy_slot_offers_nearest(full_session):
    settings = make_settings()
    mine = await _book(full_session, settings)
    await _book(full_session, settings, user=OTHER, time_="15:00")
    response = await _press(full_session, settings, "booking_move", booking_id=mine,
                            date=DAY.isoformat(), time="15:00")
    assert "занято" in response.client_texts[0]
    keyboard = json.loads(response.keyboard_json)
    times = [b["action"]["payload"]["time"] for row in keyboard["buttons"] for b in row]
    assert "15:00" not in times and times


async def test_reschedule_overlapping_itself(full_session):
    settings = make_settings(slot_step_minutes=30)
    mine = await _book(full_session, settings)
    response = await _press(full_session, settings, "booking_move", booking_id=mine,
                            date=DAY.isoformat(), time="12:30")
    assert "перенесена" in response.client_texts[0]


async def test_llm_tools_reschedule_and_list(full_session):
    settings = make_settings()
    mine = await _book(full_session, settings)
    executor = ToolExecutor(full_session, CLIENT_ID, settings=settings)
    listed = json.loads(await executor.execute("list_my_bookings", {}))
    assert [b["booking_id"] for b in listed["bookings"]] == [mine]
    moved = json.loads(await executor.execute("reschedule_booking", {
        "booking_id": mine, "date": DAY.isoformat(), "time": "17:00"}))
    assert moved["success"] and moved["was"].endswith("12:00")
    assert executor.notifications == [(mine, "rescheduled", moved["was"])]
    bad = json.loads(await executor.execute("reschedule_booking", {
        "booking_id": mine, "date": DAY.isoformat(), "time": "17:30"}))
    assert "сетки" in bad["error"] and bad["nearest_free_slots"]


async def test_llm_cancel_produces_admin_notification(full_session):
    settings = make_settings()
    mine = await _book(full_session, settings)
    openrouter = AsyncMock()
    openrouter.extract_assistant_message = OpenRouterClient.extract_assistant_message
    call = {"id": "c1", "type": "function",
            "function": {"name": "cancel_booking", "arguments": json.dumps({"booking_id": mine})}}
    openrouter.chat_completion.side_effect = [
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "Отменил."}}]},
    ]
    response = await DialogService(full_session, openrouter, settings).handle_message(
        IncomingMessage(user_id=CLIENT_ID, peer_id=CLIENT_ID, text="Отмените, пожалуйста, мою запись")
    )
    assert response.client_texts == ["Отменил."]
    notify = [a for a in response.actions if a.type == DialogActionType.NOTIFY_BOOKING]
    assert notify[0].booking_id == mine and notify[0].notify_kind == "cancelled"
