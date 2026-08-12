"""Cache adapters owned by the platform track."""

from app.platform.cache.redis import RedisCache, create_redis_cache

__all__ = ["RedisCache", "create_redis_cache"]
