"""Redis fixed-window adapter behavior and safe error mapping."""

import asyncio
from collections.abc import Awaitable
from typing import Any, cast

import pytest
from pydantic import SecretStr
from redis.exceptions import (
    AuthenticationError,
    DataError,
    RedisError,
    ResponseError,
)
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import (
    TimeoutError as RedisTimeoutError,
)

from app.config import Settings
from app.contracts.ports import ProviderError, ProviderErrorKind
from app.platform.cache.redis import FIXED_WINDOW_SCRIPT, RedisCache, create_redis_cache


class StubRedis:
    def __init__(self, result: object = 1, *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.closed = False

    async def eval(self, script: str, numkeys: int, *args: object) -> object:
        self.calls.append((script, numkeys, args))
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self) -> None:
        self.closed = True


def _cache(client: StubRedis) -> RedisCache:
    return RedisCache(cast(Any, client))


@pytest.mark.anyio
async def test_redis_counter_uses_one_atomic_script_without_extending_existing_ttl() -> None:
    client = StubRedis(2)
    cache = _cache(client)

    count = await cache.increment_fixed_window(
        key="seqret:rate:access:digest",
        window_seconds=60,
        timeout_seconds=0.2,
    )

    assert count == 2
    assert client.calls == [(FIXED_WINDOW_SCRIPT, 1, ("seqret:rate:access:digest", 60))]
    assert "INCR" in FIXED_WINDOW_SCRIPT
    assert "if count == 1" in FIXED_WINDOW_SCRIPT
    assert "EXPIRE" in FIXED_WINDOW_SCRIPT


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "kind", "retryable"),
    [
        (RedisTimeoutError(), ProviderErrorKind.DEADLINE_EXCEEDED, True),
        (RedisConnectionError(), ProviderErrorKind.UNAVAILABLE, True),
        (AuthenticationError(), ProviderErrorKind.PERMISSION_DENIED, False),
        (DataError(), ProviderErrorKind.INVALID_INPUT, False),
        (ResponseError(), ProviderErrorKind.INVALID_INPUT, False),
        (RedisError(), ProviderErrorKind.UNAVAILABLE, True),
    ],
)
async def test_redis_counter_maps_provider_failures(
    error: Exception,
    kind: ProviderErrorKind,
    retryable: bool,
) -> None:
    cache = _cache(StubRedis(error=error))

    with pytest.raises(ProviderError) as error_info:
        await cache.increment_fixed_window(key="key", window_seconds=1, timeout_seconds=0.2)

    assert error_info.value.kind is kind
    assert error_info.value.retryable is retryable


@pytest.mark.anyio
async def test_redis_counter_enforces_its_own_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raise_timeout(awaitable: Awaitable[object], timeout: float) -> object:
        awaitable.close() if hasattr(awaitable, "close") else None
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", raise_timeout)

    with pytest.raises(ProviderError) as error_info:
        await _cache(StubRedis()).increment_fixed_window(
            key="key", window_seconds=1, timeout_seconds=0.1
        )

    assert error_info.value.kind is ProviderErrorKind.DEADLINE_EXCEEDED


@pytest.mark.anyio
@pytest.mark.parametrize("result", [True, 0, -1, "1", None])
async def test_redis_counter_rejects_invalid_results(result: object) -> None:
    with pytest.raises(ProviderError) as error_info:
        await _cache(StubRedis(result)).increment_fixed_window(
            key="key", window_seconds=1, timeout_seconds=0.2
        )

    assert error_info.value.kind is ProviderErrorKind.UNAVAILABLE


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("key", "window_seconds", "timeout_seconds"),
    [("", 1, 0.2), ("key", 0, 0.2), ("key", 1, 0.0)],
)
async def test_redis_counter_validates_contract_inputs(
    key: str,
    window_seconds: int,
    timeout_seconds: float,
) -> None:
    with pytest.raises(ValueError):
        await _cache(StubRedis()).increment_fixed_window(
            key=key,
            window_seconds=window_seconds,
            timeout_seconds=timeout_seconds,
        )


@pytest.mark.anyio
async def test_redis_cache_closes_its_client() -> None:
    client = StubRedis()

    await _cache(client).close()

    assert client.closed is True


def test_redis_cache_factory_requires_and_hides_configured_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_from_url(url: str, **kwargs: object) -> StubRedis:
        captured.update(url=url, **kwargs)
        return StubRedis()

    monkeypatch.setattr("app.platform.cache.redis.Redis.from_url", fake_from_url)
    settings = Settings(
        redis_url=SecretStr("redis://cache-secret@cache.internal:6379/0"),
        redis_max_connections=7,
    )

    cache = create_redis_cache(settings)

    assert isinstance(cache, RedisCache)
    assert captured == {
        "url": "redis://cache-secret@cache.internal:6379/0",
        "decode_responses": True,
        "max_connections": 7,
    }
    assert "cache-secret" not in repr(settings)


def test_redis_cache_factory_requires_url() -> None:
    with pytest.raises(ValueError, match="SEQRET_REDIS_URL"):
        create_redis_cache(Settings())
