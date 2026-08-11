"""Environment-backed application settings shared by every runtime."""

import re
from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    """Supported deployment environments."""

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
MAX_SERVICE_NAME_LENGTH = 56
SERVICE_NAME_PATTERN = re.compile(r"^[a-z](?:[a-z0-9-]{0,54}[a-z0-9])?$")
API_PREFIX_PATTERN = re.compile(r"^/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*$")


class Settings(BaseSettings):
    """Configuration contract consumed by API, worker, and job runtimes."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        env_prefix="SEQRET_",
        extra="ignore",
        frozen=True,
        str_strip_whitespace=True,
    )

    app_name: str = Field(default="SEQRET Backend", min_length=1, max_length=100)
    service_name: str = "seqret"
    environment: AppEnvironment = AppEnvironment.LOCAL
    debug: bool = False
    log_level: LogLevel = "INFO"
    api_prefix: str = "/api/v1"

    @field_validator("service_name")
    @classmethod
    def validate_service_name(cls, value: str) -> str:
        """Keep service identity safe for logs, metrics, and deployment names."""

        if SERVICE_NAME_PATTERN.fullmatch(value) is None:
            msg = (
                "service_name must be a lowercase DNS label that starts with a letter "
                f"and is at most {MAX_SERVICE_NAME_LENGTH} characters"
            )
            raise ValueError(msg)
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        """Accept conventional case-insensitive log level input."""

        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("api_prefix")
    @classmethod
    def validate_api_prefix(cls, value: str) -> str:
        """Require one canonical absolute API prefix."""

        normalized = value.rstrip("/")
        segments = normalized.split("/")[1:]
        has_relative_segment = any(segment in {".", ".."} for segment in segments)
        if API_PREFIX_PATTERN.fullmatch(normalized) is None or has_relative_segment:
            msg = "api_prefix must be a canonical absolute path such as /api/v1"
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
