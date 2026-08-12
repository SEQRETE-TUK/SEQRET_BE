"""Tests for the shared SQLAlchemy engine and transaction boundary."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.platform.db import (
    Base,
    DatabaseConfigurationError,
    create_database_engine,
    create_session_factory,
    transactional_session,
)
from app.platform.db.dependencies import Session, get_database_session


class TransactionFailure(RuntimeError):
    """Expected error used to verify rollback."""


@pytest.fixture
def anyio_backend() -> str:
    """Run async database tests on asyncio only."""

    return "asyncio"


@pytest.mark.parametrize(
    ("database_url", "message"),
    [
        (None, "is required"),
        ("not a url", "not a valid"),
        ("sqlite+aiosqlite:///:memory:", r"must use the postgresql\+psycopg driver"),
        ("postgresql+psycopg://seqret:secret@localhost", "must select a database"),
    ],
)
def test_database_engine_rejects_invalid_configuration(
    database_url: str | None,
    message: str,
) -> None:
    settings = Settings(database_url=SecretStr(database_url) if database_url is not None else None)

    with pytest.raises(DatabaseConfigurationError, match=message):
        create_database_engine(settings)


@pytest.mark.anyio
async def test_database_engine_uses_async_postgresql_without_exposing_password() -> None:
    socket_path = "/cloudsql/seqret-staging:asia-northeast3:seqret-stg-db"
    settings = Settings(
        database_url=SecretStr(
            f"postgresql+psycopg://seqret:database-secret@/seqret?host={socket_path}"
        ),
        database_socket_path=socket_path,
        database_pool_size=3,
        database_max_overflow=4,
        database_pool_timeout_seconds=12.5,
    )

    engine = create_database_engine(settings)
    try:
        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.url.database == "seqret"
        assert engine.url.query["host"] == socket_path
        _, connect_args = engine.dialect.create_connect_args(engine.url)
        assert connect_args["host"] == socket_path
        assert "database-secret" not in str(engine.url)
        assert "database-secret" not in repr(engine)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://seqret:secret@/seqret?host=/cloudsql/other",
        ("postgresql+psycopg://seqret:secret@/seqret?host=/cloudsql/expected&hostaddr=10.0.0.5"),
        ("postgresql+psycopg://seqret:secret@/seqret?host=/cloudsql/expected&dbname=other"),
    ],
)
def test_database_engine_rejects_unexpected_socket(database_url: str) -> None:
    settings = Settings(
        database_url=SecretStr(database_url),
        database_socket_path="/cloudsql/expected",
    )

    with pytest.raises(DatabaseConfigurationError, match="DATABASE_SOCKET_PATH"):
        create_database_engine(settings)


@pytest.mark.anyio
async def test_transactional_session_commits_and_rolls_back(tmp_path: Path) -> None:
    database_path = (tmp_path / "session.sqlite3").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    metadata = MetaData()
    records = Table(
        "transaction_probe",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("value", String(50), nullable=False),
    )

    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)

        factory = create_session_factory(engine)
        async with transactional_session(factory) as session:
            await session.execute(insert(records).values(value="committed"))

        with pytest.raises(TransactionFailure, match="rollback"):
            async with transactional_session(factory) as session:
                await session.execute(insert(records).values(value="rolled-back"))
                raise TransactionFailure("rollback this transaction")

        async with engine.connect() as connection:
            result = await connection.execute(select(records.c.value).order_by(records.c.id))

        assert result.scalars().all() == ["committed"]
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_session_dependency_finishes_before_response() -> None:
    application = FastAPI()

    @application.get("/probe")
    async def probe(_session: Session) -> dict[str, bool]:
        return {"ok": True}

    async def fail_on_exit() -> AsyncIterator[None]:
        yield None
        raise TransactionFailure("commit failed")

    application.dependency_overrides[get_database_session] = fail_on_exit
    async with AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/probe")

    assert response.status_code == 500


def test_base_uses_deterministic_constraint_names() -> None:
    assert Base.metadata.naming_convention == {
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
