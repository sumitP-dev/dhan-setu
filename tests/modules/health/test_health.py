import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.modules.health import service
from tests.helpers import FakeRedis


async def test_liveness(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_ok(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


async def test_readiness_reports_redis_down(client: AsyncClient, fake_redis: FakeRedis) -> None:
    fake_redis.healthy = False

    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "service_unavailable", "message": "Unavailable: redis"}
    }


@pytest.fixture
async def unreachable_database(app: FastAPI) -> AsyncIterator[None]:
    engine = create_async_engine("sqlite+aiosqlite:////nonexistent-dir/dhan-setu.db")
    app.state.session_factory = async_sessionmaker(engine)
    yield
    await engine.dispose()


@pytest.mark.usefixtures("unreachable_database")
async def test_readiness_reports_database_down(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Unavailable: database"


async def test_readiness_times_out_slow_dependency(
    client: AsyncClient, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang() -> bool:
        await asyncio.sleep(10)
        return True

    monkeypatch.setattr(fake_redis, "ping", hang)
    monkeypatch.setattr(service, "CHECK_TIMEOUT_SECONDS", 0.01)

    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Unavailable: redis"


async def test_routes_live_under_api_v1(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 404
