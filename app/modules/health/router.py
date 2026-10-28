from fastapi import APIRouter, status

from app.common.exceptions import ServiceUnavailableError
from app.common.schemas import error_responses
from app.core.database import DbSession
from app.core.redis import RedisClient
from app.modules.health.schemas import HealthResponse, ReadinessResponse
from app.modules.health.service import check_readiness

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def liveness() -> HealthResponse:
    """The process is up. Does not touch the database or Redis."""
    return HealthResponse(status="ok")


@router.get("/ready", responses=error_responses(status.HTTP_503_SERVICE_UNAVAILABLE))
async def readiness(db: DbSession, redis: RedisClient) -> ReadinessResponse:
    """The app can reach PostgreSQL and Redis."""
    checks = await check_readiness(db, redis)
    failed = [name for name, result in checks.items() if result == "error"]
    if failed:
        raise ServiceUnavailableError(f"Unavailable: {', '.join(failed)}")
    return ReadinessResponse(status="ok", checks=checks)
