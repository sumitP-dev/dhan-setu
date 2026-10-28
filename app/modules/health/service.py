import asyncio
import logging
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.health.schemas import CheckStatus

logger = logging.getLogger(__name__)

CHECK_TIMEOUT_SECONDS = 2.0


async def _run_check(name: str, probe: Callable[[], Awaitable[object]]) -> CheckStatus:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            await probe()
    except Exception:
        logger.warning("Readiness check failed: %s", name, exc_info=True)
        return "error"
    return "ok"


async def check_readiness(db: AsyncSession, redis: Redis) -> dict[str, CheckStatus]:
    """Probe each dependency concurrently, each with its own timeout."""

    async def ping_database() -> None:
        await db.execute(text("SELECT 1"))

    async def ping_redis() -> None:
        await redis.ping()

    database, cache = await asyncio.gather(
        _run_check("database", ping_database),
        _run_check("redis", ping_redis),
    )
    return {"database": database, "redis": cache}
