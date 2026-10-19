"""DialogService tests."""

from unittest.mock import AsyncMock

from app.schemas.message import AttachmentType, DialogActionType, IncomingAttachment, IncomingMessage
from app.services.dialog_service import DialogService


async def test_wants_admin_keyword(full_session, settings):
    openrouter = AsyncMock()
    service = DialogService(full_session, openrouter, settings)

    response = await service.handle_message(
        IncomingMessage(user_id=42, peer_id=42, text="Позовите администратора")
    )
    assert response.actions[0].type == DialogActionType.TRANSFER_TO_ADMIN
    openrouter.chat_completion.assert_not_called()


async def test_build_user_content_with_photo():
    msg = IncomingMessage(
        user_id=1,
        peer_id=1,
        text="Посмотрите",
        attachments=[IncomingAttachment(type=AttachmentType.PHOTO)],
    )
    content = DialogService._build_user_content(msg)
    assert "фото" in content.lower()
