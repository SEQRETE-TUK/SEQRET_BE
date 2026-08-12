"""PostgreSQL engine, session factory, and transaction boundary."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings

POSTGRESQL_ASYNC_DRIVER = "postgresql+psycopg"


class DatabaseConfigurationError(ValueError):
    """Raised when runtime database configuration is missing or unsafe."""


def _database_url(settings: Settings) -> URL:
    configured_url = settings.database_url
    if configured_url is None:
        msg = "SEQRET_DATABASE_URL is required before creating a database engine"
        raise DatabaseConfigurationError(msg)

    try:
        url = make_url(configured_url.get_secret_value())
    except ArgumentError as error:
        msg = "SEQRET_DATABASE_URL is not a valid SQLAlchemy URL"
        raise DatabaseConfigurationError(msg) from error

    if url.drivername != POSTGRESQL_ASYNC_DRIVER:
        msg = f"SEQRET_DATABASE_URL must use the {POSTGRESQL_ASYNC_DRIVER} driver"
        raise DatabaseConfigurationError(msg)
    if not url.database:
        msg = "SEQRET_DATABASE_URL must select a database"
        raise DatabaseConfigurationError(msg)
    return url


def create_database_engine(settings: Settings) -> AsyncEngine:
    """Create a PostgreSQL async engine without exposing statement parameters."""

    return create_async_engine(
        _database_url(settings),
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
        hide_parameters=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create sessions with explicit transaction completion and no implicit expiry."""

    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )


@asynccontextmanager
async def transactional_session(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Commit one application transaction or roll it back on any exception."""

    async with factory() as session, session.begin():
        yield session
