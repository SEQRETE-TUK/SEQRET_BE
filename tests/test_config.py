"""Tests for the shared settings contract."""

import pytest
from pydantic import ValidationError

from app.config import AppEnvironment, Settings, get_settings
from app.runtime import RuntimeKind, create_runtime_context


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    """Keep process-level settings caching isolated between tests."""

    get_settings.cache_clear()


def test_settings_load_seqret_prefixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEQRET_ENVIRONMENT", "staging")
    monkeypatch.setenv("SEQRET_LOG_LEVEL", "WARNING")

    settings = get_settings()

    assert settings.environment is AppEnvironment.STAGING
    assert settings.log_level == "WARNING"


def test_settings_normalize_api_prefix() -> None:
    settings = Settings(api_prefix="/api/v1/")

    assert settings.api_prefix == "/api/v1"


def test_settings_reject_relative_api_prefix() -> None:
    with pytest.raises(ValidationError, match="api_prefix must be an absolute path"):
        Settings(api_prefix="api/v1")


def test_settings_reject_production_debug() -> None:
    with pytest.raises(ValidationError, match="debug must be disabled in production"):
        Settings(environment=AppEnvironment.PRODUCTION, debug=True)


@pytest.mark.parametrize("runtime_kind", list(RuntimeKind))
def test_each_runtime_uses_the_same_settings_contract(runtime_kind: RuntimeKind) -> None:
    settings = Settings(environment=AppEnvironment.TEST)

    context = create_runtime_context(runtime_kind, settings)

    assert context.settings is settings
    assert context.service_name == f"seqret-{runtime_kind.value}"
