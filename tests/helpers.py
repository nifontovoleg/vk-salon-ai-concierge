"""Test helpers."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


ADMIN_ID = 500
CLIENT_ID = 42


def salon_today() -> date:
    return datetime.now(ZoneInfo("Asia/Yekaterinburg")).date()


def future_day(days: int = 1) -> date:
    return salon_today() + timedelta(days=days)


def make_settings(**overrides):
    from app.config.settings import Settings

    values = dict(
        vk_group_token="t",
        vk_group_id=1,
        openrouter_api_key="k",
        salon_name="Test Salon",
        salon_address="ул. Тестовая, 1",
        salon_phone="+7 (999) 000-00-00",
        salon_working_hours="Пн-Вс 10:00-20:00",
        timezone="Asia/Yekaterinburg",
        privacy_policy_url="https://example.com/privacy",
    )
    values.update(overrides)
    return Settings(**values)


class FakeApi:
    """Records messages.send calls."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.messages = self

    async def send(self, **kwargs) -> int:
        self.sent.append(kwargs)
        return len(self.sent)

    def to(self, user_id: int) -> list[str]:
        return [m["message"] for m in self.sent if m["user_id"] == user_id]
