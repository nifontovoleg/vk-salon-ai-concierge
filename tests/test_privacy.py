"""Fix 7: consent before LLM, PII masking in logs and LLM text; photo prompt."""

import json
import logging
from unittest.mock import AsyncMock

from app.clients.openrouter import OpenRouterClient
from app.privacy import PiiLogFilter, extract_phone, mask_pii
from app.repositories import ConsentRepository, DialogRepository
from app.schemas.message import IncomingMessage
from app.services.dialog_service import DialogService
from tests.helpers import CLIENT_ID, future_day, make_settings

STRANGER = 77


def test_mask_pii():
    text = "Мой номер +7 (999) 123-45-67, запасной 8 912 000 11 22, почта olga@example.ru"
    masked = mask_pii(text)
    assert "999" not in masked and "912" not in masked and "olga@" not in masked
    assert masked.count("[телефон]") == 2 and "[email]" in masked


def test_mask_keeps_dates_and_ids():
    for text in ("2026-10-20 15:00", "20.10.2026 в 15:00", "vk.com/id1234567890", "стрижка 2500 ₽"):
        assert mask_pii(text) == text


def test_extract_phone_normalizes():
    assert extract_phone("звоните 8 (999) 123-45-67") == "+79991234567"
    assert extract_phone("9991234567") == "+79991234567"
    assert extract_phone("без номера") is None


def test_log_filter_masks():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "client %s wrote %s", (5, "+79991234567 a@b.ru"), None)
    PiiLogFilter().filter(record)
    assert record.getMessage() == "client 5 wrote [телефон] [email]"


def _llm(content="Ок"):
    openrouter = AsyncMock()
    openrouter.extract_assistant_message = OpenRouterClient.extract_assistant_message
    openrouter.chat_completion.return_value = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    return openrouter


async def _ask(session, openrouter, user, text=None, intent=None, **settings):
    return await DialogService(session, openrouter, make_settings(**settings)).handle_message(
        IncomingMessage(user_id=user, peer_id=user, text=text or "", intent=intent)
    )


async def test_without_consent_no_llm_and_nothing_stored(full_session):
    openrouter = _llm()
    response = await _ask(full_session, openrouter, STRANGER, "Посоветуйте уход за окрашенными волосами")
    openrouter.chat_completion.assert_not_called()
    assert "https://example.com/privacy" in response.client_texts[0]
    assert "consent_yes" in response.keyboard_json
    assert await DialogRepository(full_session).get_recent_messages(STRANGER) == []


async def test_without_consent_db_answers_work(full_session):
    openrouter = _llm()
    response = await _ask(full_session, openrouter, STRANGER, "Какой у вас прайс?")
    assert "2500" in response.client_texts[0]
    response = await _ask(full_session, openrouter, STRANGER, intent="slots")
    assert "Свободное время" in response.client_texts[0]
    # запись через чат требует согласия
    response = await _ask(full_session, openrouter, STRANGER, "Хочу записаться на стрижку завтра")
    assert "Согласен" in response.client_texts[0]
    openrouter.chat_completion.assert_not_called()


async def test_consent_given_then_llm(full_session):
    openrouter = _llm("Здравствуйте!")
    response = await _ask(full_session, openrouter, STRANGER, intent="consent_yes")
    assert "согласие сохранено" in response.client_texts[0]
    assert await ConsentRepository(full_session).has(STRANGER)
    await _ask(full_session, openrouter, STRANGER, "Посоветуйте уход за волосами")
    openrouter.chat_completion.assert_awaited()


async def test_consent_not_required(full_session):
    openrouter = _llm()
    await _ask(full_session, openrouter, STRANGER, "Посоветуйте уход за волосами", require_consent=False)
    openrouter.chat_completion.assert_awaited()


async def test_revoke_consent_deletes_history(full_session):
    openrouter = _llm()
    await _ask(full_session, openrouter, CLIENT_ID, "Посоветуйте уход за волосами")
    response = await _ask(full_session, openrouter, CLIENT_ID, "Отозвать согласие")
    assert "отозвано" in response.client_texts[0]
    assert not await ConsentRepository(full_session).has(CLIENT_ID)
    assert await DialogRepository(full_session).get_recent_messages(CLIENT_ID) == []


async def test_llm_gets_masked_text_but_booking_has_real_phone(full_session):
    openrouter = _llm()
    await _ask(full_session, openrouter, CLIENT_ID, "Мой телефон 8 999 123-45-67, почта o@x.ru")
    sent = json.dumps(openrouter.chat_completion.await_args.args[0], ensure_ascii=False)
    assert "123-45-67" not in sent and "o@x.ru" not in sent and "[телефон]" in sent

    call = {"id": "c1", "type": "function", "function": {"name": "create_booking", "arguments": json.dumps(
        {"service_name": "Мужская стрижка", "date": str(future_day(4)), "time": "10:00", "phone": "[телефон]"})}}
    openrouter.chat_completion.side_effect = [
        {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "Готово"}}]},
    ]
    await _ask(full_session, openrouter, CLIENT_ID, "Запишите на мужскую стрижку")
    second_history = json.dumps(openrouter.chat_completion.await_args_list[-2].args[0], ensure_ascii=False)
    assert "123-45-67" not in second_history
    from app.repositories import BookingRepository

    [booking] = await BookingRepository(full_session).list_by_user(CLIENT_ID)
    assert booking.client_phone == "+79991234567"


async def test_mask_disabled_sends_raw(full_session):
    openrouter = _llm()
    await _ask(full_session, openrouter, CLIENT_ID, "Мой телефон 8 999 123-45-67", mask_pii=False)
    sent = json.dumps(openrouter.chat_completion.await_args.args[0], ensure_ascii=False)
    assert "123-45-67" in sent


def test_system_prompt_no_photo_analysis_claim():
    from app.config.settings import BASE_DIR

    prompt = (BASE_DIR / "app" / "prompts" / "system_prompt.txt").read_text(encoding="utf-8")
    assert "предложи прислать фото" not in prompt
    assert "не анализируешь" in prompt
    assert "подбирать стрижки и образы" not in prompt
