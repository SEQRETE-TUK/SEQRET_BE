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


def test_settings_reject_invalid_redis_url() -> None:
    for redis_url in ["http://cache.internal", "redis:///0"]:
        with pytest.raises(ValidationError, match="redis_url must use redis"):
            Settings(redis_url=SecretStr(redis_url))


def test_settings_strip_human_readable_names() -> None:
    settings = Settings(app_name="  SEQRET Test API  ", service_name="  seqret-test  ")

    assert settings.app_name == "SEQRET Test API"
    assert settings.service_name == "seqret-test"


def test_settings_reject_invalid_service_name() -> None:
    for service_name in [
        "",
        "SEQRET",
        "seqret_1",
        "-seqret",
        "seqret-",
        f"s{'e' * MAX_SERVICE_NAME_LENGTH}",
    ]:
        with pytest.raises(ValidationError, match="service_name must be a lowercase DNS label"):
            Settings(service_name=service_name)


def test_runtime_service_name_stays_within_cloud_run_limit() -> None:
    settings = Settings(service_name=f"s{'e' * (MAX_SERVICE_NAME_LENGTH - 1)}")

    for runtime_kind in RuntimeKind:
        context = create_runtime_context(runtime_kind, settings)

        assert len(settings.service_name) == MAX_SERVICE_NAME_LENGTH
        assert len(context.service_name) <= CLOUD_RUN_SERVICE_NAME_MAX_LENGTH, runtime_kind


def test_settings_normalize_api_prefix() -> None:
    settings = Settings(api_prefix="/api/v1/")

    assert settings.api_prefix == "/api/v1"


def test_settings_reject_noncanonical_api_prefix() -> None:
    for api_prefix in [
        "/",
        "api/v1",
        "/api//v1",
        "/api/../v1",
        "/api/v1?debug=true",
        "/api/v1#docs",
    ]:
        with pytest.raises(ValidationError, match="api_prefix must be a canonical absolute path"):
            Settings(api_prefix=api_prefix)


def test_settings_reject_production_debug() -> None:
    with pytest.raises(ValidationError, match="debug must be disabled in production"):
        Settings(environment=AppEnvironment.PRODUCTION, debug=True)


def test_settings_reject_invalid_frontend_origin() -> None:
    for frontend_origin in [
        "http://frontend.example.com",
        "https://frontend.example.com/",
        "https://FRONTEND.example.com",
        "https://frontend.example.com:443",
        "https://user@frontend.example.com",
        "https://frontend.example.com:not-a-port",
        "https://127.1",
    ]:
        with pytest.raises(ValidationError, match="frontend_origin must be one HTTPS origin"):
            Settings(frontend_origin=frontend_origin)


def test_settings_accept_complete_pubsub_and_relay_configuration() -> None:
    settings = Settings(
        pubsub_project_id="seqret-test",
        pubsub_topic_id="domain-events.v1",
        pubsub_subscription_id="participant-notifications.v1",
        outbox_batch_size=25,
        outbox_lease_seconds=30,
        event_publish_timeout_seconds=5,
        notification_batch_size=20,
        notification_pull_timeout_seconds=4,
    )

    assert settings.pubsub_project_id == "seqret-test"
    assert settings.pubsub_topic_id == "domain-events.v1"
    assert settings.pubsub_subscription_id == "participant-notifications.v1"
    assert settings.outbox_batch_size == 25
    assert settings.notification_batch_size == 20


def test_settings_accept_complete_external_notification_configuration() -> None:
    settings = Settings(
        notification_delivery_enabled=True,
        notification_delivery_lease_seconds=30,
        notification_delivery_timeout_seconds=5,
        frontend_origin="https://seqret.example.com",
        nhn_notification_email_app_key="email-app",
        nhn_notification_email_secret_key=SecretStr("email-secret"),
        nhn_notification_email_sender_address="notice@seqret.example.com",
        nhn_notification_email_sender_name="SEQRET",
        nhn_notification_sms_app_key="sms-app",
        nhn_notification_sms_secret_key=SecretStr("sms-secret"),
        nhn_notification_sms_sender_number="0212345678",
        nhn_notification_kakao_app_key="kakao-app",
        nhn_notification_kakao_secret_key=SecretStr("kakao-secret"),
        nhn_notification_kakao_sender_key="a" * 40,
        nhn_notification_kakao_template_code="SEQRET_NOTICE",
    )

    assert settings.notification_delivery_enabled
    assert settings.notification_delivery_lease_seconds == 30
    assert settings.nhn_notification_email_secret_key is not None
    rendered = repr(settings)
    assert "email-secret" not in rendered
    assert "sms-secret" not in rendered
    assert "kakao-secret" not in rendered


def test_settings_reject_incomplete_or_unsafe_notification_configuration() -> None:
    for values in [
        {"notification_delivery_enabled": True},
        {
            "notification_delivery_lease_seconds": 10,
            "notification_delivery_timeout_seconds": 10,
        },
    ]:
        with pytest.raises(ValidationError):
            Settings.model_validate(values)


def test_settings_reject_invalid_notification_provider_values() -> None:
    cases = [
        (
            {"nhn_notification_email_secret_key": SecretStr("")},
            "secret keys must not be empty",
        ),
        (
            {"nhn_notification_email_sender_address": "not-an-email"},
            "must be an email address",
        ),
        (
            {"nhn_notification_email_sender_address": f"{'a' * 89}@example.com"},
            "at most 100 characters",
        ),
        (
            {"nhn_notification_sms_sender_number": "02-1234-5678"},
            "must contain 8 to 13 digits",
        ),
        (
            {"nhn_notification_kakao_sender_key": "!" * 40},
            "must contain 40 alphanumerics",
        ),
    ]

    for values, message in cases:
        with pytest.raises(ValidationError, match=message):
            Settings.model_validate(values)


def test_settings_accept_worker_storage_and_complete_task_dispatch_configuration() -> None:
    worker = Settings(media_bucket_name="seqret-stg-media")
    relay = Settings(
        gcp_project_id="seqret-test",
        task_queue_location="asia-northeast3",
        task_queue_name="seqret-stg-media",
        task_worker_url="https://seqret-stg-worker.run.app",
        task_invoker_service_account_email=("seqret-stg-tasks@seqret-test.iam.gserviceaccount.com"),
        background_job_batch_size=25,
        background_job_lease_seconds=30,
        task_enqueue_timeout_seconds=5,
    )

    assert worker.media_bucket_name == "seqret-stg-media"
    assert worker.storage_signing_service_account_email is None
    assert relay.task_queue_name == "seqret-stg-media"
    assert relay.background_job_batch_size == 25


def test_settings_reject_incomplete_or_invalid_media_storage() -> None:
    for values in [
        {
            "storage_signing_service_account_email": "seqret-stg-api@seqret-staging.iam.gserviceaccount.com"
        },
        {
            "media_bucket_name": "INVALID",
            "storage_signing_service_account_email": "seqret-stg-api@seqret-staging.iam.gserviceaccount.com",
        },
        {
            "media_bucket_name": "seqret-stg-media",
            "storage_signing_service_account_email": "not-a-service-account@example.com",
        },
    ]:
        with pytest.raises(ValidationError):
            Settings.model_validate(values)


def test_settings_reject_invalid_pubsub_and_relay_configuration() -> None:
    for values in [
        {"pubsub_project_id": "seqret-test"},
        {"pubsub_topic_id": "domain-events"},
        {"pubsub_subscription_id": "participant-notifications"},
        {"pubsub_project_id": "INVALID", "pubsub_topic_id": "domain-events"},
        {"pubsub_project_id": "seqret-test", "pubsub_topic_id": "no spaces"},
        {"pubsub_project_id": "seqret-test", "pubsub_topic_id": "goog-events"},
        {
            "pubsub_project_id": "seqret-test",
            "pubsub_topic_id": "domain-events",
            "pubsub_subscription_id": "no spaces",
        },
        {
            "pubsub_project_id": "seqret-test",
            "pubsub_topic_id": "domain-events",
            "pubsub_subscription_id": "goog-notifications",
        },
        {"outbox_lease_seconds": 10, "event_publish_timeout_seconds": 10},
    ]:
        with pytest.raises(ValidationError):
            Settings.model_validate(values)


def test_settings_reject_incomplete_or_invalid_task_dispatch() -> None:
    for values in [
        {"gcp_project_id": "seqret-test", "task_queue_name": "seqret-stg-media"},
        {
            "gcp_project_id": "seqret-test",
            "task_queue_location": "not-a-region",
            "task_queue_name": "seqret-stg-media",
            "task_worker_url": "https://seqret-stg-worker.run.app",
            "task_invoker_service_account_email": (
                "seqret-stg-tasks@seqret-test.iam.gserviceaccount.com"
            ),
        },
        {
            "gcp_project_id": "seqret-test",
            "task_queue_location": "asia-northeast3",
            "task_queue_name": "invalid_queue",
            "task_worker_url": "https://seqret-stg-worker.run.app",
            "task_invoker_service_account_email": (
                "seqret-stg-tasks@seqret-test.iam.gserviceaccount.com"
            ),
        },
        {
            "gcp_project_id": "seqret-test",
            "task_queue_location": "asia-northeast3",
            "task_queue_name": "seqret-stg-media",
            "task_worker_url": "http://seqret-stg-worker.run.app",
            "task_invoker_service_account_email": (
                "seqret-stg-tasks@seqret-test.iam.gserviceaccount.com"
            ),
        },
        {
            "gcp_project_id": "seqret-test",
            "task_queue_location": "asia-northeast3",
            "task_queue_name": "seqret-stg-media",
            "task_worker_url": "https://seqret-stg-worker.run.app:not-a-port",
            "task_invoker_service_account_email": (
                "seqret-stg-tasks@seqret-test.iam.gserviceaccount.com"
            ),
        },
        {
            "gcp_project_id": "seqret-test",
            "task_queue_location": "asia-northeast3",
            "task_queue_name": "seqret-stg-media",
            "task_worker_url": "https://seqret-stg-worker.run.app/path",
            "task_invoker_service_account_email": (
                "seqret-stg-tasks@seqret-test.iam.gserviceaccount.com"
            ),
        },
        {
            "gcp_project_id": "seqret-test",
            "task_queue_location": "asia-northeast3",
            "task_queue_name": "seqret-stg-media",
            "task_worker_url": "https://seqret-stg-worker.run.app",
            "task_invoker_service_account_email": "invalid",
        },
        {"background_job_lease_seconds": 10, "task_enqueue_timeout_seconds": 10},
    ]:
        with pytest.raises(ValidationError):
            Settings.model_validate(values)


def test_settings_reject_invalid_observability_configuration() -> None:
    cases = [
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
    ]

    for values, message in cases:
        with pytest.raises(ValidationError, match=message):
            Settings.model_validate(values)


def test_each_runtime_uses_the_same_settings_contract() -> None:
    settings = Settings(environment=AppEnvironment.TEST)

    for runtime_kind in RuntimeKind:
        context = create_runtime_context(runtime_kind, settings)

        assert context.settings is settings, runtime_kind
        assert context.service_name == f"seqret-{runtime_kind.value}"
