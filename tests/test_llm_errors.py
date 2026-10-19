"""Fix 6: invalid tool JSON and unexpected LLM errors never leave the client without a reply."""

import json
from unittest.mock import AsyncMock

import pytest

from app.clients.openrouter import OpenRouterClient, OpenRouterError
from app.schemas.message import IncomingMessage
from app.services.dialog_service import DialogService
from tests.helpers import CLIENT_ID, make_settings


def _openrouter(*responses):
    openrouter = AsyncMock()
    openrouter.extract_assistant_message = OpenRouterClient.extract_assistant_message
    openrouter.chat_completion.side_effect = list(responses)
    return openrouter


def _tool_call(name, arguments):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": arguments}}]}}]}


async def _ask(session, openrouter, text="Подскажите, что посоветуете для тонких волос?"):
    return await DialogService(session, openrouter, make_settings()).handle_message(
        IncomingMessage(user_id=CLIENT_ID, peer_id=CLIENT_ID, text=text)
    )


async def test_invalid_tool_json_is_reported_to_model(full_session):
    openrouter = _openrouter(
        _tool_call("get_services", "{not json"),
        {"choices": [{"message": {"role": "assistant", "content": "Вот наши услуги."}}]},
    )
    response = await _ask(full_session, openrouter)
    assert response.client_texts == ["Вот наши услуги."]
    second_call_messages = openrouter.chat_completion.await_args_list[1].args[0]
    tool_msg = second_call_messages[-1]
    assert tool_msg["role"] == "tool" and "JSON" in json.loads(tool_msg["content"])["error"]


@pytest.mark.parametrize(
    "error",
    [OpenRouterError("503"), RuntimeError("boom"), KeyError("choices"), json.JSONDecodeError("x", "", 0)],
)
async def test_any_llm_error_falls_back(full_session, error):
    openrouter = _openrouter(error)
    response = await _ask(full_session, openrouter, text="Сколько стоит стрижка? Хочу понять заранее")
    [text] = response.client_texts
    assert "2500" in text


async def test_unknown_answer_gets_generic_reply(full_session):
    response = await _ask(full_session, _openrouter({"choices": []}))
    [text] = response.client_texts
    assert "AI временно недоступен" in text


async def test_tool_args_not_object(full_session):
    openrouter = _openrouter(
        _tool_call("create_booking", "[1, 2]"),
        {"choices": [{"message": {"role": "assistant", "content": "Уточните детали."}}]},
    )
    assert (await _ask(full_session, openrouter)).client_texts == ["Уточните детали."]


async def test_booking_kept_when_llm_fails_after_tool(full_session):
    from tests.helpers import future_day

    call = _tool_call("create_booking", json.dumps(
        {"service_name": "Мужская стрижка", "date": str(future_day(3)), "time": "11:00", "phone": "+79991234567"}))
    response = await _ask(full_session, _openrouter(call, RuntimeError("timeout")))
    assert "ждёт подтверждения" in response.client_texts[0]
    assert any(a.booking_id for a in response.actions)


def test_handler_wraps_unexpected_errors():
    import inspect

    from app.handlers import messages

    source = inspect.getsource(messages.register_handlers)
    assert "except Exception" in source and "ERROR_TEXT" in source

