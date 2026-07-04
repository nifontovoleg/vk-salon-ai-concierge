"""Fallback reply tests."""

import pytest

from app.config.settings import Settings
from app.services.fallback_service import FallbackService


@pytest.mark.asyncio
async def test_fallback_services(seeded_session):
    settings = Settings(
        vk_group_token="t",
        vk_group_id=1,
        openrouter_api_key="k",
        salon_name="Test Salon",
    )
    service = FallbackService(seeded_session, settings, user_vk_id=1)
    reply = await service.try_reply("Расскажите про услуги и цены")
    assert reply is not None
    assert "2500" in reply


@pytest.mark.asyncio
async def test_fallback_book_intent(seeded_session):
    settings = Settings(
        vk_group_token="t",
        vk_group_id=1,
        openrouter_api_key="k",
        salon_name="Test Salon",
    )
    service = FallbackService(seeded_session, settings, user_vk_id=1)
    reply = await service.reply_by_intent("book")
    assert "Запись" in reply
    assert "2500" in reply
    assert "услугу" in reply.lower()


@pytest.mark.asyncio
async def test_book_text_not_services(seeded_session):
    settings = Settings(
        vk_group_token="t",
        vk_group_id=1,
        openrouter_api_key="k",
        salon_name="Test Salon",
    )
    service = FallbackService(seeded_session, settings, user_vk_id=1)
    reply = await service.try_reply("Хочу записаться на услугу")
    assert reply is not None
    assert "Запись" in reply or "запис" in reply.lower()
    assert reply.startswith("📅")
