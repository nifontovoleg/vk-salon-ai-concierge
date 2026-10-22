"""Application configuration."""

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Centralized application settings loaded from environment."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    vk_group_token: str = Field(..., description="VK community access token")
    vk_group_id: int = Field(..., description="VK community numeric ID")

    openrouter_api_key: str = Field(..., description="OpenRouter API key")
    openrouter_model: str = Field(
        default="openai/gpt-4o-mini",
        description="LLM model identifier on OpenRouter",
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        description="OpenRouter API base URL",
    )

    database_url: str = Field(
        default=f"sqlite+aiosqlite:///{BASE_DIR / 'data' / 'salon.db'}",
        description="Async SQLAlchemy database URL",
    )

    log_level: str = Field(default="INFO")
    salon_name: str = Field(default="Салон красоты")
    salon_address: str = Field(default="")
    salon_phone: str = Field(default="")
    salon_working_hours: str = Field(default="")

    seed_admin_vk_ids: str = Field(
        default="",
        description="Comma-separated VK user IDs for initial admins",
    )

    timezone: str = Field(default="Asia/Yekaterinburg", description="IANA timezone of the salon")
    slot_step_minutes: int = Field(default=30, ge=5, le=240, description="Booking grid step")
    salon_days_off: str = Field(default="", description="Salon holidays: 2026-12-31, 2027-01-01..2027-01-03")
    master_schedules: str = Field(
        default="", description="Per-master hours: «Анна=Пн-Ср 10:00-18:00 | Дмитрий=Чт-Вс 12:00-20:00»"
    )
    master_days_off: str = Field(
        default="", description="Per-master days off: «Анна=2026-11-02, 2026-11-10..2026-11-15»"
    )
    cancel_min_hours: float = Field(
        default=2.0, ge=0, description="Client can't cancel/reschedule online closer than this"
    )

    solo_mode: bool = Field(
        default=False,
        description="Solo master: instant reply to new clients, prices only from DB, no master choice",
    )

    require_consent: bool = Field(default=True, description="Ask PD consent before LLM dialog")
    privacy_policy_url: str = Field(default="", description="Link to personal data policy")
    mask_pii: bool = Field(default=True, description="Mask phones/emails in text sent to LLM")
    anonymize_ai: bool = Field(
        default=True,
        description="Replace all detected PII with tokens like [ТЕЛЕФОН_1] before LLM and restore in replies",
    )

    dialog_history_limit: int = Field(default=20, ge=1, le=100)
    openrouter_timeout: float = Field(default=60.0, ge=5.0)
    openrouter_max_tokens: int = Field(default=1024, ge=128)

    @field_validator("vk_group_token", "openrouter_api_key")
    @classmethod
    def strip_secrets(cls, value: str) -> str:
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = value.strip() or "Asia/Yekaterinburg"
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown TIMEZONE: {value}") from exc
        return value

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        """Resolve relative SQLite paths against project root."""
        prefix = "sqlite+aiosqlite:///./"
        if value.startswith(prefix):
            rel_path = value[len(prefix) :]
            abs_path = (BASE_DIR / rel_path).resolve()
            return f"sqlite+aiosqlite:///{abs_path.as_posix()}"
        return value

    @property
    def prompts_dir(self) -> Path:
        return BASE_DIR / "app" / "prompts"

    @property
    def data_dir(self) -> Path:
        return BASE_DIR / "data"

    @staticmethod
    def _per_master(raw: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in (raw or "").split("|"):
            if "=" not in item:
                continue
            name, value = item.split("=", 1)
            if name.strip():
                result[name.strip()] = value.strip()
        return result

    @property
    def master_schedule_map(self) -> dict[str, str]:
        return self._per_master(self.master_schedules)

    @property
    def master_days_off_map(self) -> dict[str, str]:
        return self._per_master(self.master_days_off)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def seed_admin_ids(self) -> list[int]:
        if not self.seed_admin_vk_ids.strip():
            return []
        return [int(item.strip()) for item in self.seed_admin_vk_ids.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
