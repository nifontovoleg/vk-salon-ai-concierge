"""SOLO_MODE: instant first reply, prices only from DB, booking by buttons, no master choice."""

import json
from unittest.mock import AsyncMock

from app.clients.openrouter import OpenRouterClient
from app.db.models import Booking, SoloClient
from app.schemas.message import DialogActionType, IncomingMessage
from app.services.dialog_service import DialogService
from app.services.solo_service import guard_prices, quoted_prices
from sqlalchemy import select
from tests.helpers import CLIENT_ID, make_settings

NEW_CLIENT = 77


def _solo():
    return make_settings(solo_mode=True)


def _openrouter(*texts):
    openrouter = AsyncMock()
    openrouter.extract_assistant_message = OpenRouterClient.extract_assistant_message
    openrouter.chat_completion.side_effect = [
        {"choices": [{"message": {"role": "assistant", "content": t}}]} for t in texts
    ]
    return openrouter


async def _send(session, text="", user=CLIENT_ID, intent=None, payload=None, openrouter=None, settings=None):
    openrouter = openrouter or AsyncMock()
    service = DialogService(session, openrouter, settings or _solo())
    msg = IncomingMessage(user_id=user, peer_id=user, text=text, intent=intent, payload=payload or {})
    return await service.handle_message(msg)


def _buttons(response):
    kb = json.loads(response.keyboard_json)
    out = []
    for row in kb["buttons"]:
        for b in row:
            action = dict(b["action"])
            if isinstance(action.get("payload"), str):
                action["payload"] = json.loads(action["payload"])
            out.append(action)
    return out


async def test_first_message_instant_without_llm_and_consent(full_session):
    openrouter = AsyncMock()
    response = await _send(full_session, "Здравствуйте, сколько стоит покраска?", user=NEW_CLIENT, openrouter=openrouter)
    openrouter.chat_completion.assert_not_called()
    text = response.client_texts[0]
    assert "Женская стрижка — 2500 ₽" in text and "Мужская стрижка — 1500 ₽" in text
    assert "Ближайшие свободные окна" in text
    payloads = [b["payload"] for b in _buttons(response)]
    assert {p["action"] for p in payloads} == {"solo_service"}
    assert "Анна" not in text and "Дмитрий" not in text  # no master step
    # second message is no longer "first": goes to normal flow (consent request)
    again = await _send(full_session, "А покраска есть?", user=NEW_CLIENT, openrouter=openrouter)
    assert "согласи" in again.client_texts[0].lower()


async def test_solo_off_keeps_old_behaviour(full_session):
    response = await _send(full_session, "сколько стоит покраска волос в тон", user=NEW_CLIENT, settings=make_settings())
    assert "Ближайшие свободные окна" not in response.client_texts[0]
    assert await full_session.get(SoloClient, NEW_CLIENT) is None


async def test_full_booking_in_dialog_with_consent_flow(full_session):
    await _send(full_session, "привет", user=NEW_CLIENT)
    first = await _send(full_session, user=NEW_CLIENT, intent="book")
    sid = _buttons(first)[0]["payload"]["service_id"]
    slots = await _send(full_session, user=NEW_CLIENT, intent="solo_service", payload={"service_id": sid})
    slot = _buttons(slots)[0]["payload"]
    assert slot["action"] == "solo_slot"
    chosen = await _send(full_session, user=NEW_CLIENT, intent="solo_slot", payload=slot)
    assert "Согласен" in json.dumps(json.loads(chosen.keyboard_json), ensure_ascii=False)
    consent = await _send(full_session, user=NEW_CLIENT, intent="consent_yes")
    assert "телефон" in consent.client_texts[0]
    openrouter = AsyncMock()
    done = await _send(full_session, "+7 912 345-67-89", user=NEW_CLIENT, openrouter=openrouter)
    openrouter.chat_completion.assert_not_called()
    assert "ждёт подтверждения" in done.client_texts[0]
    assert any(a.type == DialogActionType.NOTIFY_BOOKING for a in done.actions)
    booking = (await full_session.execute(select(Booking).where(Booking.user_vk_id == NEW_CLIENT))).scalar_one()
    assert booking.client_phone and booking.booking_time.strftime("%H:%M") == slot["time"]
    assert booking.master_id is not None  # assigned automatically


async def test_taken_slot_offers_nearest(full_session):
    first = await _send(full_session, user=CLIENT_ID, intent="book")
    sid = _buttons(first)[0]["payload"]["service_id"]
    slots = await _send(full_session, user=CLIENT_ID, intent="solo_service", payload={"service_id": sid})
    slot = _buttons(slots)[0]["payload"]
    # both masters get booked on that slot by others
    for other in (1001, 1002):
        await _send(full_session, user=other, intent="solo_slot", payload=slot)
        await _send(full_session, user=other, intent="consent_yes")
        await _send(full_session, "89120000000", user=other)
    await _send(full_session, user=CLIENT_ID, intent="solo_slot", payload=slot)
    response = await _send(full_session, "мой номер 89123456789", user=CLIENT_ID)
    assert "занято" in response.client_texts[0]
    assert all(b["payload"]["action"] == "solo_slot" for b in _buttons(response))


def test_quoted_prices_parsing():
    assert quoted_prices("Стрижка 2 500 ₽, укладка 1500 руб., маникюр от 900р") == {2500, 1500, 900}
    assert quoted_prices("Длительность 60 мин, в 10:00, тел. +7 912 345-67-89") == set()


async def test_guard_passes_db_prices(full_session):
    text = "Женская стрижка стоит 2500 ₽."
    assert await guard_prices(full_session, text) == text


async def test_ai_never_quotes_price_not_in_db(full_session):
    """Модель «придумала» цену на покраску — клиент её не увидит, получит прайс из базы."""
    await _send(full_session, "привет")  # first message — instant reply from DB
    openrouter = _openrouter("Покраска стоит 3000 ₽, записать вас?")
    response = await _send(full_session, "Сколько стоит покраска волос?", openrouter=openrouter)
    text = response.client_texts[-1]
    assert "3000" not in text
    assert quoted_prices(text) <= {2500, 1500}
    assert "администратор" in text and "Женская стрижка — 2500 ₽" in text


async def test_solo_prompt_and_no_master_tool(full_session):
    await _send(full_session, "привет")  # first message — instant reply
    openrouter = _openrouter("Стрижка стоит 1500 ₽.")
    response = await _send(full_session, "Сколько стоит мужская стрижка?", openrouter=openrouter)
    assert response.client_texts == ["Стрижка стоит 1500 ₽."]
    call = openrouter.chat_completion.await_args_list[0]
    tools = {t["function"]["name"] for t in call.kwargs["tools"]}
    assert "get_masters" not in tools and "get_services" in tools
    assert "ТОЛЬКО из get_services" in call.args[0][0]["content"]
