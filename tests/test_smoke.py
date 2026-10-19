"""Smoke tests: booking tool, button intents, LLM tool loop, instance lock."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from app.config.settings import Settings
from app.repositories import BookingRepository
from app.schemas.message import DialogActionType, IncomingMessage
from app.services.dialog_service import DialogService
from app.services.tools import ToolExecutor
from app.utils.instance_lock import InstanceLock

TOMORROW = str(date.today() + timedelta(days=1))


def _settings() -> Settings:
    return Settings(
        vk_group_token="t",
        vk_group_id=1,
        openrouter_api_key="k",
        salon_name="Test Salon",
    )


def _llm_reply(content: str | None = None, tool_calls: list | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


async def test_create_booking_and_conflict(seeded_session):
    executor = ToolExecutor(seeded_session, user_vk_id=7)
    args = {"service_name": "женская стрижка", "date": TOMORROW, "time": "12:00", "phone": "+70000000000"}

    first = json.loads(await executor.execute("create_booking", args))
    assert first["success"] is True
    assert first["service"] == "Женская стрижка"

    second = json.loads(await executor.execute("create_booking", args))
    assert "занято" in second["error"]

    slots = json.loads(await executor.execute("get_available_slots", {"date": TOMORROW}))
    assert "12:00" not in slots["available_slots"]
    assert "11:00" in slots["available_slots"]


async def test_create_booking_unknown_service(seeded_session):
    executor = ToolExecutor(seeded_session, user_vk_id=7)
    result = json.loads(
        await executor.execute(
            "create_booking",
            {"service_name": "Пилинг", "date": TOMORROW, "time": "12:00", "phone": "+70000000000"},
        )
    )
    assert "не найдена" in result["error"]


async def test_unknown_tool(seeded_session):
    result = json.loads(await ToolExecutor(seeded_session, 1).execute("drop_db", {}))
    assert "Unknown tool" in result["error"]


@pytest.mark.parametrize("intent", ["book", "services", "slots"])
async def test_buttons_do_not_call_llm(seeded_session, intent):
    openrouter = AsyncMock()
    service = DialogService(seeded_session, openrouter, _settings())
    response = await service.handle_message(
        IncomingMessage(user_id=5, peer_id=5, text="кнопка", intent=intent)
    )
    assert response.actions[0].type == DialogActionType.REPLY
    openrouter.chat_completion.assert_not_called()


async def test_resume_after_admin_transfer(seeded_session):
    openrouter = AsyncMock()
    service = DialogService(seeded_session, openrouter, _settings())
    await service.handle_message(IncomingMessage(user_id=5, peer_id=5, text="", intent="admin"))
    response = await service.handle_message(IncomingMessage(user_id=5, peer_id=5, text="отмена"))
    assert response.actions[0].type == DialogActionType.REPLY
    assert "продолжаем" in response.actions[0].text


async def test_llm_tool_loop_creates_booking(seeded_session):
    openrouter = AsyncMock()
    openrouter.extract_assistant_message = lambda r: r["choices"][0]["message"]
    openrouter.chat_completion.side_effect = [
        _llm_reply(
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "create_booking",
                        "arguments": json.dumps(
                            {
                                "service_name": "Мужская стрижка",
                                "date": TOMORROW,
                                "time": "15:00",
                                "phone": "+70000000000",
                            }
                        ),
                    },
                }
            ]
        ),
        _llm_reply("Записал вас на завтра в 15:00."),
    ]
    service = DialogService(seeded_session, openrouter, _settings())
    response = await service.handle_message(
        IncomingMessage(user_id=9, peer_id=9, text="Запишите на мужскую стрижку завтра в 15, +70000000000")
    )
    assert response.actions[0].text == "Записал вас на завтра в 15:00."
    bookings = await BookingRepository(seeded_session).list_by_user(9)
    assert len(bookings) == 1
    assert openrouter.chat_completion.await_count == 2


async def test_llm_failure_uses_fallback(seeded_session):
    from app.clients.openrouter import OpenRouterError

    openrouter = AsyncMock()
    openrouter.chat_completion.side_effect = OpenRouterError("down", status_code=503)
    service = DialogService(seeded_session, openrouter, _settings())
    response = await service.handle_message(
        IncomingMessage(user_id=3, peer_id=3, text="Сколько стоит стрижка у вас, подскажите пожалуйста")
    )
    assert "2500" in response.actions[0].text


def test_instance_lock_stale_own_pid(tmp_path):
    lock_path = tmp_path / "bot.lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    lock = InstanceLock(lock_path)
    lock.acquire()
    lock.release()
    assert not lock_path.exists()
