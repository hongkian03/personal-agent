"""Application configuration. Secrets are never included in model context."""

from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PERSONAL_AGENT_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    environment: str = "development"
    timezone: str = "America/New_York"
    owner_id: str = Field(default="personal", min_length=1, max_length=128)
    data_dir: Path = Path(".data")
    persona_path: Path = Path("config/agent.md")
    skill_bundle_path: Path = Path("config/skills.json")
    database_url: SecretStr | None = None
    api_token: SecretStr | None = None
    model_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("PERSONAL_AGENT_MODEL_API_KEY", "OPENAI_API_KEY"),
    )
    model: str = "gpt-6-luna"
    model_timeout_seconds: float = Field(default=60, gt=0, le=600)
    chat_timeout_seconds: float = Field(default=180, gt=0, le=1800)
    progress_enabled: bool = True
    progress_ack_delay_seconds: float = Field(default=5, gt=0, le=1800)
    progress_interval_seconds: float = Field(default=30, gt=0, le=1800)
    progress_max_updates: int = Field(default=5, ge=0, le=100)
    tool_timeout_seconds: float = Field(default=30, gt=0, le=300)
    max_tool_rounds: int = Field(default=8, ge=1, le=32)
    max_output_tokens: int = Field(default=2048, ge=128, le=32768)
    max_history_chars: int = Field(default=120000, ge=1000, le=1000000)
    web_search_enabled: bool = True
    web_search_context_size: Literal["low", "medium", "high"] = "medium"
    web_search_max_calls: int = Field(default=3, ge=1, le=10)
    log_level: str = "INFO"
    ibkr_flex_token: SecretStr | None = None
    ibkr_flex_query_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")
    ibkr_account_ids: str = Field(default="", max_length=2048)
    portfolio_sync_interval_seconds: float = Field(default=3600, ge=300, le=86400)
    portfolio_morning_enabled: bool = True
    portfolio_focus_symbols: str = Field(default="", max_length=2048)
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
        "ibkr_flex_token",
        "photon_api_key",
        "photon_webhook_secret",
        "alpaca_api_key",
        "alpaca_secret_key",
        mode="before",
    )
    @classmethod
    def empty_secret(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("ibkr_flex_query_id", mode="before")
    @classmethod
    def empty_query_id(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def ibkr_credentials_together(self):
        if bool(self.ibkr_flex_token) != bool(self.ibkr_flex_query_id):
            raise ValueError("Set IBKR_FLEX_TOKEN and IBKR_FLEX_QUERY_ID together")
        return self

    @property
    def ibkr_accounts(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(s.strip() for s in self.ibkr_account_ids.split(",") if s.strip())
        )

    @property
    def focused_symbols(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                s.strip().upper() for s in self.portfolio_focus_symbols.split(",") if s.strip()
            )
        )

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value
