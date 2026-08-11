"""Environment-backed application settings shared by every runtime."""

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    """Supported deployment environments."""

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Configuration contract consumed by API, worker, and job runtimes."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        env_prefix="SEQRET_",
        extra="ignore",
        frozen=True,
    )

    app_name: str = "SEQRET Backend"
    service_name: str = "seqret"
    environment: AppEnvironment = AppEnvironment.LOCAL
    debug: bool = False
    log_level: LogLevel = "INFO"
    api_prefix: str = "/api/v1"

    @field_validator("api_prefix")
    @classmethod
    def validate_api_prefix(cls, value: str) -> str:
        """Require one canonical absolute API prefix."""

        normalized = value.rstrip("/")
        if not normalized or not normalized.startswith("/"):
            msg = "api_prefix must be an absolute path such as /api/v1"
            raise ValueError(msg)
        return normalized

    @model_validator(mode="after")
    def reject_production_debug(self) -> Self:
        """Prevent accidental debug responses in production."""

        if self.environment is AppEnvironment.PRODUCTION and self.debug:
            msg = "debug must be disabled in production"
            raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache the process-wide settings instance."""

    return Settings()
