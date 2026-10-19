"""VK keyboards."""

from vkbottle import Keyboard, KeyboardButtonColor, Text


def main_keyboard() -> str:
    """Return JSON keyboard with quick actions."""
    return (
        Keyboard(inline=False, one_time=False)
        .add(Text("Записаться", payload={"action": "book"}), color=KeyboardButtonColor.POSITIVE)
        .add(Text("Услуги и цены", payload={"action": "services"}), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text("Свободное время", payload={"action": "slots"}), color=KeyboardButtonColor.PRIMARY)
        .add(Text("Администратор", payload={"action": "admin"}), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def handoff_keyboard() -> str:
    """Client keyboard while the dialog is handed off to an administrator."""
    return (
        Keyboard(inline=False, one_time=False)
        .add(Text("Вернуться к боту", payload={"action": "resume"}), color=KeyboardButtonColor.PRIMARY)
        .get_json()
    )


def consent_keyboard() -> str:
    """Inline buttons for personal data consent."""
    return (
        Keyboard(inline=True)
        .add(Text("Согласен", payload={"action": "consent_yes"}), color=KeyboardButtonColor.POSITIVE)
        .add(Text("Не согласен", payload={"action": "consent_no"}), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def admin_notify_keyboard(user_vk_id: int) -> str:
    """Keyboard for admins to take or dismiss a transfer request."""
    return (
        Keyboard(inline=True)
        .add(
            Text("Взять диалог", payload={"action": "take_dialog", "user_id": user_vk_id}),
            color=KeyboardButtonColor.POSITIVE,
        )
        .add(
            Text("Завершить", payload={"action": "release_dialog", "user_id": user_vk_id}),
            color=KeyboardButtonColor.SECONDARY,
        )
        .get_json()
    )


def admin_release_keyboard(user_vk_id: int) -> str:
    """Inline button under forwarded client messages."""
    return (
        Keyboard(inline=True)
        .add(
            Text("Завершить", payload={"action": "release_dialog", "user_id": user_vk_id}),
            color=KeyboardButtonColor.SECONDARY,
        )
        .get_json()
    )


def booking_admin_keyboard(booking_id: int) -> str:
    """Inline buttons to confirm or cancel a pending booking."""
    return (
        Keyboard(inline=True)
        .add(
            Text("Подтвердить", payload={"action": "booking_confirm", "booking_id": booking_id}),
            color=KeyboardButtonColor.POSITIVE,
        )
        .add(
            Text("Отменить", payload={"action": "booking_cancel", "booking_id": booking_id}),
            color=KeyboardButtonColor.NEGATIVE,
        )
        .get_json()
    )
