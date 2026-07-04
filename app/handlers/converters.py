"""Convert VK events to platform-agnostic messages."""

from vkbottle.bot import Message

from app.schemas.message import AttachmentType, IncomingAttachment, IncomingMessage


def message_from_vk(event: Message) -> IncomingMessage:
    """Map VKBottle Message to IncomingMessage."""
    attachments: list[IncomingAttachment] = []

    if event.attachments:
        for att in event.attachments:
            if att.photo:
                attachments.append(
                    IncomingAttachment(
                        type=AttachmentType.PHOTO,
                        url=None,
                        description="photo",
                    )
                )
            else:
                attachments.append(
                    IncomingAttachment(
                        type=AttachmentType.OTHER,
                        description=type(att).__name__,
                    )
                )

    return IncomingMessage(
        user_id=event.from_id,
        peer_id=event.peer_id,
        text=event.text or "",
        attachments=attachments,
        raw_payload={"message_id": event.id, "conversation_message_id": event.conversation_message_id},
    )
