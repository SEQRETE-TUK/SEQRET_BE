"""Cloud Run Job entrypoint for the deployment migration gate."""

from alembic import command
from alembic.config import Config

from app.config import Settings
from app.platform.db.session import validated_database_url
from app.platform.observability import (
    create_observability,
    new_correlation_context,
    use_correlation,
)
from app.runtime import RuntimeKind, create_runtime_context


def run(settings: Settings | None = None) -> None:
    """Upgrade the single Alembic head while emitting one deployment span."""

    resolved = settings or Settings()
    if resolved.database_url is None:
        raise RuntimeError("Database configuration is required for migrations")
    observability = create_observability(create_runtime_context(RuntimeKind.JOB, resolved))
    try:
        with (
            observability.tracer.start_as_current_span("database.migrate"),
            use_correlation(new_correlation_context()),
        ):
            config = Config("alembic.ini")
            validated_database_url(resolved)
            database_url = resolved.database_url.get_secret_value().replace("%", "%%")
            config.set_main_option("sqlalchemy.url", database_url)
            command.upgrade(config, "head")
            observability.logger.info(
                "Database migration completed",
                extra={"event": "database_migration_complete", "outcome": "success"},
            )
    finally:
        observability.shutdown()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
