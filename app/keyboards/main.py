"""VK inline keyboards."""

from vkbottle import Keyboard, Text, KeyboardButtonColor


def main_keyboard() -> str:
    """Return JSON keyboard with quick actions."""
    keyboard = (
        Keyboard(inline=False, one_time=False)
        .add(Text("Записаться", payload={"action": "book"}), color=KeyboardButtonColor.POSITIVE)
        .add(Text("Услуги и цены", payload={"action": "services"}), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text("Свободное время", payload={"action": "slots"}), color=KeyboardButtonColor.PRIMARY)
        .add(Text("Администратор", payload={"action": "admin"}), color=KeyboardButtonColor.SECONDARY)
        .get_json()
    )
    return keyboard


def admin_notify_keyboard(user_vk_id: int) -> str:
    """Keyboard for admins to take or dismiss a transfer request."""
    keyboard = (
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
    return keyboard
