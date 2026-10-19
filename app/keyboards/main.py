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
        .row()
        .add(Text("Мои записи", payload={"action": "my_bookings"}), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )


def my_bookings_keyboard(booking_ids: list[int]) -> str:
    """Inline «Отменить #N» / «Перенести #N» for up to 5 bookings."""
    keyboard = Keyboard(inline=True)
    for index, booking_id in enumerate(booking_ids[:5]):
        if index:
            keyboard.row()
        keyboard.add(
            Text(f"Отменить #{booking_id}", payload={"action": "booking_client_cancel", "booking_id": booking_id}),
            color=KeyboardButtonColor.NEGATIVE,
        )
        keyboard.add(
            Text(f"Перенести #{booking_id}", payload={"action": "booking_reschedule", "booking_id": booking_id}),
            color=KeyboardButtonColor.PRIMARY,
        )
    return keyboard.get_json()


def reschedule_keyboard(booking_id: int, slots: list) -> str:
    """Inline buttons with new times (up to 8, two per row)."""
    keyboard = Keyboard(inline=True)
    for index, (day, start) in enumerate(slots[:8]):
        if index and index % 2 == 0:
            keyboard.row()
        keyboard.add(
            Text(
                f"{day.strftime('%d.%m')} {start.strftime('%H:%M')}",
                payload={
                    "action": "booking_move",
                    "booking_id": booking_id,
                    "date": day.isoformat(),
                    "time": start.strftime("%H:%M"),
                },
            ),
            color=KeyboardButtonColor.PRIMARY,
        )
    return keyboard.get_json()


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
