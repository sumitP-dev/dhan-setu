from datetime import timedelta

from redis.asyncio import Redis

from app.common.exceptions import TooManyRequestsError


class AttemptLimiter:
    """Counts failed attempts per identifier in Redis. Once max_attempts is reached the
    identifier is blocked until the window (started by the first failure) expires."""

    def __init__(
        self, redis: Redis, *, namespace: str, max_attempts: int, window: timedelta
    ) -> None:
        self._redis = redis
        self._namespace = namespace
        self._max_attempts = max_attempts
        self._window_seconds = int(window.total_seconds())

    def _key(self, identifier: str) -> str:
        return f"attempts:{self._namespace}:{identifier}"

    async def ensure_allowed(self, identifier: str) -> None:
        key = self._key(identifier)
        attempts = await self._redis.get(key)
        if attempts is not None and int(attempts) >= self._max_attempts:
            retry_after = await self._redis.ttl(key)
            raise TooManyRequestsError(
                "Too many failed attempts. Try again later.",
                code="too_many_attempts",
                retry_after_seconds=max(retry_after, 1),
            )

    async def record_failure(self, identifier: str) -> None:
        key = self._key(identifier)
        await self._redis.incr(key)
        # nx: only the first failure starts the window. Also self-heals a key that
        # somehow lost its TTL, so nobody is locked out forever.
        await self._redis.expire(key, self._window_seconds, nx=True)

    async def reset(self, identifier: str) -> None:
        await self._redis.delete(self._key(identifier))
