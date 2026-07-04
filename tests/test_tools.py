"""Tool executor tests."""

import json
from datetime import date

import pytest

from app.services.tools import ToolExecutor


@pytest.mark.asyncio
async def test_get_services_tool(seeded_session):
    executor = ToolExecutor(seeded_session, user_vk_id=100)
    result = json.loads(await executor.execute("get_services", {}))
    assert len(result["services"]) == 2
    assert result["services"][0]["price"] == 2500


@pytest.mark.asyncio
async def test_get_available_slots_tool(seeded_session):
    executor = ToolExecutor(seeded_session, user_vk_id=100)
    result = json.loads(
        await executor.execute("get_available_slots", {"date": str(date.today())})
    )
    assert "available_slots" in result
    assert len(result["available_slots"]) > 0


@pytest.mark.asyncio
async def test_transfer_to_admin_tool(seeded_session):
    executor = ToolExecutor(seeded_session, user_vk_id=100)
    result = json.loads(await executor.execute("transfer_to_admin", {"reason": "Тест"}))
    assert result["success"] is True
    assert executor.transfer_requested is True
