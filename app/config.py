"""Environment-backed application settings shared by every runtime."""

import re
from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    """Supported deployment environments."""

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
# Cloud Run service names are limited to 49 characters. Reserve the longest
# runtime suffix so one shared base name remains deployable for every runtime.
CLOUD_RUN_SERVICE_NAME_MAX_LENGTH = 49
LONGEST_RUNTIME_SUFFIX_LENGTH = len("-worker")
MAX_SERVICE_NAME_LENGTH = CLOUD_RUN_SERVICE_NAME_MAX_LENGTH - LONGEST_RUNTIME_SUFFIX_LENGTH
SERVICE_NAME_PATTERN = re.compile(
    rf"^[a-z](?:[a-z0-9-]{{0,{MAX_SERVICE_NAME_LENGTH - 2}}}[a-z0-9])?$"
)
API_PREFIX_PATTERN = re.compile(r"^/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*$")
GCP_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
FRONTEND_HOST_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
    r"\.[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
PUBSUB_TOPIC_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._~+%-]{2,254}$")
TASK_QUEUE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,99}$")
MEDIA_BUCKET_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,61}[a-z0-9])$")
SERVICE_ACCOUNT_EMAIL_PATTERN = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@"
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
)


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
    frontend_origin: str | None = None
    database_url: SecretStr | None = Field(default=None, repr=False)
    database_socket_path: str | None = Field(default=None, min_length=1, max_length=107)
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_timeout_seconds: float = Field(default=30.0, gt=0, le=300.0)
    redis_url: SecretStr | None = Field(default=None, repr=False)
    redis_max_connections: int = Field(default=10, ge=1, le=100)
    cache_timeout_seconds: float = Field(default=0.2, gt=0, le=5.0)
    access_rate_limit_requests: int = Field(default=120, ge=1, le=10_000)
    access_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3_600)
    pubsub_project_id: str | None = None
    pubsub_topic_id: str | None = None
    pubsub_subscription_id: str | None = None
    outbox_batch_size: int = Field(default=100, ge=1, le=100)
    outbox_lease_seconds: int = Field(default=60, ge=1, le=600)
    event_publish_timeout_seconds: float = Field(default=10.0, gt=0, le=300.0)
    notification_batch_size: int = Field(default=100, ge=1, le=100)
    notification_pull_timeout_seconds: float = Field(default=10.0, gt=0, le=30.0)
    task_queue_location: str | None = None
    task_queue_name: str | None = None
    task_worker_url: str | None = None
    task_invoker_service_account_email: str | None = Field(default=None, max_length=100)
    background_job_batch_size: int = Field(default=100, ge=1, le=100)
    background_job_lease_seconds: int = Field(default=60, ge=1, le=600)
    task_enqueue_timeout_seconds: float = Field(default=10.0, gt=0, le=300.0)
    media_retention_days: int | None = Field(default=None, ge=1, le=3_650)
    media_bucket_name: str | None = Field(default=None, min_length=3, max_length=63)
    storage_signing_service_account_email: str | None = Field(default=None, max_length=100)
    otel_enabled: bool = False
    otel_exporter_otlp_traces_endpoint: AnyHttpUrl | None = None
    otel_trace_sample_ratio: float = Field(default=0.1, ge=0.0, le=1.0)
    gcp_project_id: str | None = None

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
    def validate_cross_field_settings(self) -> Self:
        """Reject unsafe runtime setting combinations."""

        if self.environment is AppEnvironment.PRODUCTION and self.debug:
            msg = "debug must be disabled in production"
            raise ValueError(msg)
        if self.redis_url is not None:
            parsed_redis_url = urlsplit(self.redis_url.get_secret_value())
            if (
                parsed_redis_url.scheme not in {"redis", "rediss"}
                or parsed_redis_url.hostname is None
            ):
                msg = "redis_url must use redis:// or rediss:// and include a host"
                raise ValueError(msg)
        if self.frontend_origin is not None:
            msg = "frontend_origin must be one HTTPS origin without credentials or a path"
            origin = urlsplit(self.frontend_origin)
            try:
                port = origin.port
            except ValueError as error:
                raise ValueError(msg) from error
            if (
                origin.scheme != "https"
                or origin.hostname is None
                or FRONTEND_HOST_PATTERN.fullmatch(origin.hostname) is None
                or origin.path
                or origin.query
                or origin.fragment
                or origin.username is not None
                or origin.password is not None
                or port is not None
                or self.frontend_origin != f"https://{origin.hostname}"
            ):
                raise ValueError(msg)
        if (self.pubsub_project_id is None) != (self.pubsub_topic_id is None):
            msg = "pubsub_project_id and pubsub_topic_id must be configured together"
            raise ValueError(msg)
        if self.pubsub_subscription_id is not None and self.pubsub_topic_id is None:
            msg = "pubsub_subscription_id requires pubsub_project_id and pubsub_topic_id"
            raise ValueError(msg)
        if (
            self.storage_signing_service_account_email is not None
            and self.media_bucket_name is None
        ):
            msg = "storage_signing_service_account_email requires media_bucket_name"
            raise ValueError(msg)
        if (
            self.media_bucket_name is not None
            and MEDIA_BUCKET_NAME_PATTERN.fullmatch(self.media_bucket_name) is None
        ):
            msg = "media_bucket_name must be a canonical Cloud Storage bucket name"
            raise ValueError(msg)
        if (
            self.storage_signing_service_account_email is not None
            and SERVICE_ACCOUNT_EMAIL_PATTERN.fullmatch(self.storage_signing_service_account_email)
            is None
        ):
            msg = "storage_signing_service_account_email must be a service-account email"
            raise ValueError(msg)
        task_settings = (
            self.task_queue_location,
            self.task_queue_name,
            self.task_worker_url,
            self.task_invoker_service_account_email,
        )
        if any(value is not None for value in task_settings) and (
            self.gcp_project_id is None or any(value is None for value in task_settings)
        ):
            msg = "Cloud Tasks runtime settings and gcp_project_id must be configured together"
            raise ValueError(msg)
        if self.task_queue_location is not None and not re.fullmatch(
            r"[a-z]+-[a-z]+[0-9]+", self.task_queue_location
        ):
            raise ValueError("task_queue_location must be a canonical GCP region")
        if (
            self.task_queue_name is not None
            and TASK_QUEUE_ID_PATTERN.fullmatch(self.task_queue_name) is None
        ):
            raise ValueError("task_queue_name must be a valid Cloud Tasks queue ID")
        if self.task_worker_url is not None:
            worker_origin = urlsplit(self.task_worker_url)
            try:
                worker_port = worker_origin.port
            except ValueError as error:
                raise ValueError("task_worker_url must be one canonical HTTPS origin") from error
            if (
                worker_origin.scheme != "https"
                or worker_origin.hostname is None
                or worker_origin.path
                or worker_origin.query
                or worker_origin.fragment
                or worker_origin.username is not None
                or worker_origin.password is not None
                or worker_port is not None
                or self.task_worker_url != f"https://{worker_origin.hostname}"
            ):
                raise ValueError("task_worker_url must be one canonical HTTPS origin")
        if (
            self.task_invoker_service_account_email is not None
            and SERVICE_ACCOUNT_EMAIL_PATTERN.fullmatch(self.task_invoker_service_account_email)
            is None
        ):
            raise ValueError("task_invoker_service_account_email must be a service-account email")
        if (
            self.pubsub_project_id is not None
            and GCP_PROJECT_ID_PATTERN.fullmatch(self.pubsub_project_id) is None
        ):
            msg = "pubsub_project_id must be a valid GCP project ID"
            raise ValueError(msg)
        if (
            self.pubsub_topic_id is not None
            and PUBSUB_TOPIC_ID_PATTERN.fullmatch(self.pubsub_topic_id) is None
        ):
            msg = "pubsub_topic_id must be a valid Pub/Sub topic ID"
            raise ValueError(msg)
        if self.pubsub_topic_id is not None and self.pubsub_topic_id.lower().startswith("goog"):
            msg = "pubsub_topic_id must not start with goog"
            raise ValueError(msg)
        if (
            self.pubsub_subscription_id is not None
            and PUBSUB_TOPIC_ID_PATTERN.fullmatch(self.pubsub_subscription_id) is None
        ):
            msg = "pubsub_subscription_id must be a valid Pub/Sub subscription ID"
            raise ValueError(msg)
        if (
            self.pubsub_subscription_id is not None
            and self.pubsub_subscription_id.lower().startswith("goog")
        ):
            msg = "pubsub_subscription_id must not start with goog"
            raise ValueError(msg)
        if (
            self.gcp_project_id is not None
            and GCP_PROJECT_ID_PATTERN.fullmatch(self.gcp_project_id) is None
        ):
            msg = "gcp_project_id must be a valid GCP project ID"
            raise ValueError(msg)
        if self.otel_enabled and self.otel_exporter_otlp_traces_endpoint is None:
            msg = "otel_exporter_otlp_traces_endpoint is required when OTel is enabled"
            raise ValueError(msg)
        if (
            self.environment in {AppEnvironment.STAGING, AppEnvironment.PRODUCTION}
            and self.otel_enabled
            and self.gcp_project_id is None
        ):
            msg = "gcp_project_id is required for deployed OTel correlation"
            raise ValueError(msg)
        if self.outbox_lease_seconds <= self.event_publish_timeout_seconds:
            msg = "outbox_lease_seconds must exceed event_publish_timeout_seconds"
            raise ValueError(msg)
        if self.background_job_lease_seconds <= self.task_enqueue_timeout_seconds:
            msg = "background_job_lease_seconds must exceed task_enqueue_timeout_seconds"
            raise ValueError(msg)
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache the process-wide settings instance."""

    return Settings()
