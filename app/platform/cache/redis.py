"""Redis implementation of the atomic fixed-window counter contract."""

import asyncio
from typing import Any

from redis.asyncio import Redis
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

FIXED_WINDOW_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
""".strip()


class RedisCache:
    """One process-wide Redis client backed by its shared connection pool."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def increment_fixed_window(
        self,
        *,
        key: str,
        window_seconds: int,
        timeout_seconds: float,
    ) -> int:
        if not key:
            raise ValueError("key must not be empty")
        if window_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("window_seconds and timeout_seconds must be positive")

        try:
            result: Any = await asyncio.wait_for(
                self._client.eval(FIXED_WINDOW_SCRIPT, 1, key, window_seconds),
                timeout=timeout_seconds,
            )
        except (TimeoutError, RedisTimeoutError) as error:
            raise ProviderError(
                ProviderErrorKind.DEADLINE_EXCEEDED,
                "cache request exceeded its deadline",
                retryable=True,
            ) from error
        except AuthenticationError as error:
            raise ProviderError(
                ProviderErrorKind.PERMISSION_DENIED,
                "cache authentication failed",
                retryable=False,
            ) from error
        except RedisConnectionError as error:
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "cache is unavailable",
                retryable=True,
            ) from error
        except (DataError, ResponseError) as error:
            raise ProviderError(
                ProviderErrorKind.INVALID_INPUT,
                "cache rejected the fixed-window command",
                retryable=False,
            ) from error
        except RedisError as error:
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "cache request failed",
                retryable=True,
            ) from error

        if isinstance(result, bool) or not isinstance(result, int) or result < 1:
            raise ProviderError(
                ProviderErrorKind.UNAVAILABLE,
                "cache returned an invalid counter",
                retryable=True,
            )
        return int(result)

    async def close(self) -> None:
        """Close the client and its process-owned connection pool."""

        await self._client.aclose()


def create_redis_cache(settings: Settings) -> RedisCache:
    """Create a lazy Redis client from validated secret settings."""

    configured_url = settings.redis_url
    if configured_url is None:
        msg = "SEQRET_REDIS_URL is required before creating a Redis cache"
        raise ValueError(msg)
    client = Redis.from_url(
        configured_url.get_secret_value(),
        decode_responses=True,
        max_connections=settings.redis_max_connections,
    )
    return RedisCache(client)
