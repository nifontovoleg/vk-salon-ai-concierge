"""Tool executor tests."""

import json

from app.services.tools import ToolExecutor
from tests.helpers import future_day


async def test_get_services_tool(seeded_session, settings):
    executor = ToolExecutor(seeded_session, user_vk_id=100, settings=settings)
    result = json.loads(await executor.execute("get_services", {}))
    assert len(result["services"]) == 2
    assert result["services"][0]["price"] == 2500


async def test_get_available_slots_tool(seeded_session, settings):
    executor = ToolExecutor(seeded_session, user_vk_id=100, settings=settings)
    result = json.loads(
        await executor.execute("get_available_slots", {"date": str(future_day())})
    )
    assert result["available_slots"][0] == "10:00"
    assert result["available_slots"][-1] == "19:00"


async def test_transfer_to_admin_tool(full_session, settings):
    executor = ToolExecutor(full_session, user_vk_id=100, settings=settings)
    result = json.loads(await executor.execute("transfer_to_admin", {"reason": "Тест"}))
    assert result["success"] is True
    assert executor.transfer_requested is True
