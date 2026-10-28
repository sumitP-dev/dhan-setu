from datetime import timedelta
from typing import TYPE_CHECKING, cast

import pytest

from app.common.exceptions import TooManyRequestsError
from app.core.rate_limit import AttemptLimiter
from tests.helpers import FakeRedis

if TYPE_CHECKING:
    from redis.asyncio import Redis


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def limiter(redis: FakeRedis) -> AttemptLimiter:
    return AttemptLimiter(
        cast("Redis", redis), namespace="t", max_attempts=3, window=timedelta(minutes=15)
    )


async def test_blocks_after_max_failures(limiter: AttemptLimiter) -> None:
    for _ in range(3):
        await limiter.ensure_allowed("k")
        await limiter.record_failure("k")

    with pytest.raises(TooManyRequestsError) as exc_info:
        await limiter.ensure_allowed("k")

    assert exc_info.value.code == "too_many_attempts"
    assert exc_info.value.headers is not None
    assert 0 < int(exc_info.value.headers["Retry-After"]) <= 15 * 60


async def test_keys_are_independent(limiter: AttemptLimiter) -> None:
    for _ in range(3):
        await limiter.record_failure("a")

    await limiter.ensure_allowed("b")


async def test_reset_clears_failures(limiter: AttemptLimiter) -> None:
    for _ in range(3):
        await limiter.record_failure("k")

    await limiter.reset("k")

    await limiter.ensure_allowed("k")


async def test_unblocks_when_window_expires(limiter: AttemptLimiter, redis: FakeRedis) -> None:
    for _ in range(3):
        await limiter.record_failure("k")

    redis.expire_now("attempts:t:")

    await limiter.ensure_allowed("k")


async def test_window_starts_at_first_failure(limiter: AttemptLimiter, redis: FakeRedis) -> None:
    await limiter.record_failure("k")
    first_ttl = await redis.ttl("attempts:t:k")

    await limiter.record_failure("k")

    assert await redis.ttl("attempts:t:k") <= first_ttl
