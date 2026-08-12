"""Tests for the shared settings contract."""

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

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


@pytest.mark.parametrize("runtime_kind", list(RuntimeKind))
def test_each_runtime_uses_the_same_settings_contract(runtime_kind: RuntimeKind) -> None:
    settings = Settings(environment=AppEnvironment.TEST)

    context = create_runtime_context(runtime_kind, settings)

    assert context.settings is settings
    assert context.service_name == f"seqret-{runtime_kind.value}"
