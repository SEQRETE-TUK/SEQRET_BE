"""Alembic environment for the single linear SEQRET schema."""

from logging.config import fileConfig
from typing import cast

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

from app.config import Settings
from app.modules.access.models import ParticipantAccessToken
from app.modules.capture.models import MediaAsset
from app.modules.completion.models import AuditEvent
from app.modules.notification.models import NotificationDelivery
from app.modules.scope.models import ScopeApproval
from app.platform.event_bus.models import OutboxEvent

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_REGISTERED_MODELS = (
    ParticipantAccessToken,
    MediaAsset,
    ScopeApproval,
    AuditEvent,
    OutboxEvent,
    NotificationDelivery,
)
target_metadata = _REGISTERED_MODELS[0].metadata


def _database_url() -> str:
    configured_url = config.get_main_option("sqlalchemy.url").strip()
    if configured_url:
        return configured_url

    settings = Settings()
    if settings.database_url is None:
        msg = "SEQRET_DATABASE_URL is required to run migrations"
        raise RuntimeError(msg)
    return settings.database_url.get_secret_value()


def _configure_context(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_server_default=True,
        compare_type=True,
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Render migration SQL without creating an engine."""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        compare_server_default=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
        literal_binds=True,
        transaction_per_migration=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations through an injected connection or a short-lived engine."""

    injected_connection = config.attributes.get("connection")
    if injected_connection is not None:
        _configure_context(cast(Connection, injected_connection))
        return

    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _configure_context(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
