"""Processed message deduplication tests."""

import pytest

from app.repositories import ProcessedMessageRepository


@pytest.mark.asyncio
async def test_try_claim_first_time(session):
    repo = ProcessedMessageRepository(session)
    assert await repo.try_claim(peer_id=100, vk_message_id=42) is True
    await session.commit()


@pytest.mark.asyncio
async def test_try_claim_duplicate(session):
    repo = ProcessedMessageRepository(session)
    assert await repo.try_claim(peer_id=100, vk_message_id=42) is True
    await session.commit()

    repo2 = ProcessedMessageRepository(session)
    assert await repo2.try_claim(peer_id=100, vk_message_id=42) is False
