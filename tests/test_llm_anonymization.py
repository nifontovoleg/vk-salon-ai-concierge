"""ANONYMIZE_AI: сырые ПДн клиента не уходят в модель, ответы и аргументы инструментов восстанавливаются."""

import json
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.clients.openrouter import OpenRouterClient
from app.db.models import Booking
from app.privacy import LlmPrivacy
from app.repositories import DialogRepository
from app.schemas.message import IncomingMessage
from app.services.dialog_service import DialogService
from app.services.solo_service import quoted_prices
from tests.helpers import CLIENT_ID, future_day, make_settings

RAW = ("8 999 123-45-67", "9991234567", "Ольга Смирнова", "Смирнова", "olga.s@example.ru")
TEXT = "Здравствуйте, меня зовут Ольга Смирнова, телефон 8 999 123-45-67, почта olga.s@example.ru"


def _reply(content=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


def _mock(*replies):
    openrouter = AsyncMock()
    openrouter.extract_assistant_message = OpenRouterClient.extract_assistant_message
    openrouter.chat_completion.side_effect = list(replies)
    return openrouter


def _sent(openrouter) -> str:
    return json.dumps([c.args[0] for c in openrouter.chat_completion.await_args_list], ensure_ascii=False)


def _assert_clean(openrouter):
    sent = _sent(openrouter)
    for raw in RAW:
        assert raw not in sent, raw


async def _ask(session, openrouter, text, **settings):
    return await DialogService(session, openrouter, make_settings(**settings)).handle_message(
        IncomingMessage(user_id=CLIENT_ID, peer_id=CLIENT_ID, text=text)
    )


async def test_no_raw_pii_reaches_llm_and_reply_restored(full_session):
    openrouter = _mock(_reply("Спасибо, [ИМЯ_1]! Перезвоним на [ТЕЛЕФОН_1], письмо придёт на [EMAIL_1]."))
    response = await _ask(full_session, openrouter, TEXT)
    _assert_clean(openrouter)
    sent = _sent(openrouter)
    assert "[ИМЯ_1]" in sent and "[ТЕЛЕФОН_1]" in sent and "[EMAIL_1]" in sent
    assert response.client_texts == [
        "Спасибо, Ольга Смирнова! Перезвоним на 8 999 123-45-67, письмо придёт на olga.s@example.ru."
    ]
    # в базе ответ хранится как есть, уже с настоящими данными
    history = await DialogRepository(full_session).get_recent_messages(CLIENT_ID)
    assert "Ольга Смирнова" in history[-1].content


async def test_tool_args_restored_before_db_and_tool_results_masked(full_session):
    call = {"id": "c1", "type": "function", "function": {"name": "create_booking", "arguments": json.dumps({
        "service_name": "Мужская стрижка", "date": str(future_day(4)), "time": "10:00",
        "phone": "[ТЕЛЕФОН_1]", "client_name": "[ИМЯ_1]"}, ensure_ascii=False)}}
    openrouter = _mock(_reply(tool_calls=[call]), _reply("Записала вас, [ИМЯ_1]. Ждите подтверждения."))
    response = await _ask(full_session, openrouter, TEXT + ". Запишите на мужскую стрижку")
    _assert_clean(openrouter)  # включая результат create_booking во втором запросе
    [booking] = (await full_session.execute(select(Booking))).scalars().all()
    assert booking.client_phone == "+79991234567"
    assert booking.client_name == "Ольга Смирнова"
    assert response.client_texts[0] == "Записала вас, Ольга Смирнова. Ждите подтверждения."


async def test_history_is_masked_with_consistent_tokens(full_session):
    openrouter = _mock(_reply("Здравствуйте, [ИМЯ_1]!"), _reply("Конечно, [ИМЯ_1]."))
    await _ask(full_session, openrouter, TEXT)
    await _ask(full_session, openrouter, "А можно на вечер?")
    second = openrouter.chat_completion.await_args_list[-1].args[0]
    _assert_clean(openrouter)
    assistant = [m["content"] for m in second if m["role"] == "assistant"]
    assert assistant == ["Здравствуйте, [ИМЯ_1]!"]


async def test_unknown_token_from_model_is_neutralized(full_session):
    openrouter = _mock(_reply("Передам [ИМЯ_7] ваш вопрос."))
    response = await _ask(full_session, openrouter, "Посоветуйте уход за волосами")
    assert response.client_texts == ["Передам данные клиента ваш вопрос."]


async def test_mask_pii_false_does_not_disable_anonymizer(full_session):
    openrouter = _mock(_reply("Ок"))
    await _ask(full_session, openrouter, TEXT, mask_pii=False)
    _assert_clean(openrouter)


async def test_anonymize_off_falls_back_to_legacy_labels(full_session):
    openrouter = _mock(_reply("Ок"))
    await _ask(full_session, openrouter, TEXT, anonymize_ai=False)
    sent = _sent(openrouter)
    assert "123-45-67" not in sent and "[телефон]" in sent and "[email]" in sent
    assert "Ольга Смирнова" in sent  # прежний режим имена не скрывал


async def test_solo_price_guard_still_works(full_session):
    settings = dict(solo_mode=True)
    await _ask(full_session, AsyncMock(), "привет", **settings)  # мгновенный ответ без модели
    openrouter = _mock(_reply("[ИМЯ_1], покраска стоит 3000 ₽."))
    response = await _ask(full_session, openrouter, TEXT + ". Сколько стоит покраска?", **settings)
    _assert_clean(openrouter)
    text = response.client_texts[-1]
    assert "3000" not in text and quoted_prices(text) <= {2500, 1500}

    openrouter = _mock(_reply("[ИМЯ_1], женская стрижка стоит 2500 ₽."))
    response = await _ask(full_session, openrouter, "Меня зовут Ольга Смирнова. А стрижка?", **settings)
    assert response.client_texts[-1] == "Ольга Смирнова, женская стрижка стоит 2500 ₽."


def test_llm_privacy_known_phone_and_restore_args():
    privacy = LlmPrivacy(anonymize_ai=True, mask=True, known=[{"type": "ТЕЛЕФОН", "value": "+79991234567"}])
    masked = privacy.mask("Мой номер +79991234567")
    assert "9991234567" not in masked
    assert privacy.restore({"phone": "[ТЕЛЕФОН_1]", "n": 3}) == {"phone": "+79991234567", "n": 3}
