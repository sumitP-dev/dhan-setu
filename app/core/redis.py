from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis

from app.core.config import Settings


def create_redis_client(settings: Settings) -> Redis:
    # Connects lazily on the first command.
    return Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


RedisClient = Annotated[Redis, Depends(get_redis)]
