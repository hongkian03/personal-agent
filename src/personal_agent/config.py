"""Application configuration. Secrets are never included in model context."""

from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PERSONAL_AGENT_", env_file=".env", extra="ignore", populate_by_name=True
    )

    environment: str = "development"
    timezone: str = "America/New_York"
    owner_id: str = Field(default="personal", min_length=1, max_length=128)
    data_dir: Path = Path(".data")
    database_url: SecretStr | None = None
    api_token: SecretStr | None = None
    model_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("PERSONAL_AGENT_MODEL_API_KEY", "OPENAI_API_KEY"),
    )
    model: str = "gpt-4.1-mini"
    model_timeout_seconds: float = Field(default=60, gt=0, le=600)
    chat_timeout_seconds: float = Field(default=180, gt=0, le=1800)
    tool_timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_tool_rounds: int = Field(default=8, ge=1, le=32)
    max_output_tokens: int = Field(default=2048, ge=128, le=32768)
    max_history_chars: int = Field(default=120000, ge=1000, le=1000000)
    log_level: str = "INFO"
    photon_project_id: str | None = None
    photon_api_key: SecretStr | None = None
    photon_webhook_secret: SecretStr | None = None
    allowed_sender_id: str | None = None
    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None

    @field_validator(
        "database_url",
        "api_token",
        "model_api_key",
        "photon_api_key",
        "photon_webhook_secret",
        "alpaca_api_key",
        "alpaca_secret_key",
        mode="before",
    )
    @classmethod
    def empty_secret(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value
