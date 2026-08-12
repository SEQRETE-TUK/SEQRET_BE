"""Tests for the shared settings contract."""

from collections.abc import Iterator

import pytest
from pydantic import SecretStr, ValidationError

from app.config import (
    CLOUD_RUN_SERVICE_NAME_MAX_LENGTH,
    MAX_SERVICE_NAME_LENGTH,
    AppEnvironment,
    Settings,
    get_settings,
)
from app.runtime import RuntimeKind, create_runtime_context


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Iterator[None]:
    """Keep process-level settings caching isolated between tests."""

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_settings_load_seqret_prefixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEQRET_ENVIRONMENT", "staging")
    monkeypatch.setenv("SEQRET_LOG_LEVEL", " warning ")

    settings = get_settings()

    assert settings.environment is AppEnvironment.STAGING
    assert settings.log_level == "WARNING"


def test_settings_reject_non_string_log_level() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"log_level": 20})


def test_get_settings_caches_one_immutable_instance() -> None:
    assert get_settings() is get_settings()


def test_database_url_is_hidden_from_settings_representation() -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+psycopg://seqret:database-secret@localhost/seqret")
    )

    assert settings.database_url is not None
    assert settings.database_url.get_secret_value().endswith("@localhost/seqret")
    assert "database-secret" not in repr(settings)


def test_redis_url_is_hidden_and_validated() -> None:
    settings = Settings(redis_url=SecretStr("rediss://cache-secret@cache.internal:6379/0"))

    assert settings.redis_url is not None
    assert settings.redis_url.get_secret_value().startswith("rediss://")
    assert "cache-secret" not in repr(settings)


@pytest.mark.parametrize(
    "redis_url",
    ["http://cache.internal", "redis:///0", "cache.internal:6379"],
)
def test_settings_reject_invalid_redis_url(redis_url: str) -> None:
    with pytest.raises(ValidationError, match="redis_url must use redis"):
        Settings(redis_url=SecretStr(redis_url))


def test_settings_strip_human_readable_names() -> None:
    settings = Settings(app_name="  SEQRET Test API  ", service_name="  seqret-test  ")

    assert settings.app_name == "SEQRET Test API"
    assert settings.service_name == "seqret-test"


@pytest.mark.parametrize(
    "service_name",
    [
        "",
        "SEQRET",
        "seqret_1",
        "-seqret",
        "seqret-",
        "seqret/worker",
        f"s{'e' * MAX_SERVICE_NAME_LENGTH}",
    ],
)
def test_settings_reject_invalid_service_name(service_name: str) -> None:
    with pytest.raises(ValidationError, match="service_name must be a lowercase DNS label"):
        Settings(service_name=service_name)


@pytest.mark.parametrize("runtime_kind", list(RuntimeKind))
def test_runtime_service_name_stays_within_cloud_run_limit(
    runtime_kind: RuntimeKind,
) -> None:
    settings = Settings(service_name=f"s{'e' * (MAX_SERVICE_NAME_LENGTH - 1)}")

    context = create_runtime_context(runtime_kind, settings)

    assert len(settings.service_name) == MAX_SERVICE_NAME_LENGTH
    assert len(context.service_name) <= CLOUD_RUN_SERVICE_NAME_MAX_LENGTH


def test_settings_normalize_api_prefix() -> None:
    settings = Settings(api_prefix="/api/v1/")

    assert settings.api_prefix == "/api/v1"


@pytest.mark.parametrize(
    "api_prefix",
    ["/", "api/v1", "/api//v1", "/api/../v1", "/api/v1?debug=true", "/api/v1#docs"],
)
def test_settings_reject_noncanonical_api_prefix(api_prefix: str) -> None:
    with pytest.raises(ValidationError, match="api_prefix must be a canonical absolute path"):
        Settings(api_prefix=api_prefix)


def test_settings_reject_production_debug() -> None:
    with pytest.raises(ValidationError, match="debug must be disabled in production"):
        Settings(environment=AppEnvironment.PRODUCTION, debug=True)


def test_settings_accept_complete_pubsub_and_relay_configuration() -> None:
    settings = Settings(
        pubsub_project_id="seqret-test",
        pubsub_topic_id="domain-events.v1",
        outbox_batch_size=25,
        outbox_lease_seconds=30,
        event_publish_timeout_seconds=5,
    )

    assert settings.pubsub_project_id == "seqret-test"
    assert settings.pubsub_topic_id == "domain-events.v1"
    assert settings.outbox_batch_size == 25


@pytest.mark.parametrize(
    "values",
    [
        {"pubsub_project_id": "seqret-test"},
        {"pubsub_topic_id": "domain-events"},
        {"pubsub_project_id": "INVALID", "pubsub_topic_id": "domain-events"},
        {"pubsub_project_id": "seqret-test", "pubsub_topic_id": "no spaces"},
        {"pubsub_project_id": "seqret-test", "pubsub_topic_id": "goog-events"},
        {"outbox_lease_seconds": 10, "event_publish_timeout_seconds": 10},
    ],
)
def test_settings_reject_invalid_pubsub_and_relay_configuration(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


@pytest.mark.parametrize(
    "values, message",
    [
        ({"gcp_project_id": "INVALID"}, "gcp_project_id must be a valid"),
        ({"otel_enabled": True}, "otel_exporter_otlp_traces_endpoint is required"),
        (
            {
                "environment": AppEnvironment.STAGING,
                "otel_enabled": True,
                "otel_exporter_otlp_traces_endpoint": "https://collector.example/v1/traces",
            },
            "gcp_project_id is required",
        ),
    ],
)
def test_settings_reject_invalid_observability_configuration(
    values: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings.model_validate(values)


@pytest.mark.parametrize("runtime_kind", list(RuntimeKind))
def test_each_runtime_uses_the_same_settings_contract(runtime_kind: RuntimeKind) -> None:
    settings = Settings(environment=AppEnvironment.TEST)

    context = create_runtime_context(runtime_kind, settings)

    assert context.settings is settings
    assert context.service_name == f"seqret-{runtime_kind.value}"
