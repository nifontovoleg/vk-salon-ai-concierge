"""DialogService tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.message import DialogActionType, IncomingMessage
from app.services.dialog_service import DialogService


@pytest.mark.asyncio
async def test_wants_admin_keyword(seeded_session):
    settings = MagicMock()
    settings.dialog_history_limit = 20
    settings.prompts_dir = MagicMock()
    settings.prompts_dir.__truediv__ = MagicMock(return_value=MagicMock())
    settings.prompts_dir.__truediv__.return_value.read_text.return_value = (
        "Salon: {salon_name} {salon_address} {salon_phone} {salon_working_hours}"
    )
    settings.salon_name = "Test"
    settings.salon_address = ""
    settings.salon_phone = ""
    settings.salon_working_hours = ""

    openrouter = AsyncMock()
    service = DialogService(seeded_session, openrouter, settings)

    response = await service.handle_message(
        IncomingMessage(user_id=42, peer_id=42, text="Позовите администратора")
    )
    assert response.actions[0].type == DialogActionType.TRANSFER_TO_ADMIN
    openrouter.chat_completion.assert_not_called()


@pytest.mark.asyncio
async def test_build_user_content_with_photo():
    from app.services.dialog_service import DialogService
    from app.schemas.message import AttachmentType, IncomingAttachment

    msg = IncomingMessage(
        user_id=1,
        peer_id=1,
        text="Посмотрите",
        attachments=[IncomingAttachment(type=AttachmentType.PHOTO)],
    )
    content = DialogService._build_user_content(msg)
    assert "фото" in content.lower()
