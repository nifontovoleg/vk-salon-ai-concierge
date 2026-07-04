"""Transport-agnostic message schemas."""

from dataclasses import dataclass, field
from enum import StrEnum


class AttachmentType(StrEnum):
    PHOTO = "photo"
    DOCUMENT = "document"
    OTHER = "other"


@dataclass(slots=True)
class IncomingAttachment:
    """Abstract attachment from any messaging platform."""

    type: AttachmentType
    url: str | None = None
    description: str | None = None


@dataclass(slots=True)
class IncomingMessage:
    """Platform-agnostic incoming user message."""

    user_id: int
    peer_id: int
    text: str
    attachments: list[IncomingAttachment] = field(default_factory=list)
    raw_payload: dict | None = None
    intent: str | None = None


class DialogActionType(StrEnum):
    REPLY = "reply"
    TRANSFER_TO_ADMIN = "transfer_to_admin"
    NOOP = "noop"


@dataclass(slots=True)
class DialogAction:
    """Action returned by DialogService for the transport layer."""

    type: DialogActionType
    text: str = ""
    notify_admin: bool = False
    transfer_reason: str | None = None


@dataclass(slots=True)
class DialogResponse:
    """Complete response from DialogService."""

    actions: list[DialogAction] = field(default_factory=list)
    keyboard_json: str | None = None

    @classmethod
    def reply(cls, text: str, keyboard_json: str | None = None) -> "DialogResponse":
        return cls(
            actions=[DialogAction(type=DialogActionType.REPLY, text=text)],
            keyboard_json=keyboard_json,
        )

    @classmethod
    def transfer_to_admin(cls, text: str, reason: str | None = None) -> "DialogResponse":
        return cls(
            actions=[
                DialogAction(
                    type=DialogActionType.TRANSFER_TO_ADMIN,
                    text=text,
                    notify_admin=True,
                    transfer_reason=reason,
                )
            ],
        )
