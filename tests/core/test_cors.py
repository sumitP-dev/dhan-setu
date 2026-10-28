from httpx import ASGITransport, AsyncClient

from app.main import create_app
from tests.helpers import make_settings

PREFLIGHT_HEADERS = {
    "Origin": "http://localhost:3000",
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "Authorization, Idempotency-Key",
}


async def _preflight(cors_origins: list[str]) -> dict[str, str]:
    app = create_app(make_settings(cors_origins=cors_origins))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.options("/api/v1/health", headers=PREFLIGHT_HEADERS)
    return dict(response.headers)


async def test_cors_off_by_default() -> None:
    headers = await _preflight([])

    assert "access-control-allow-origin" not in headers


async def test_cors_allows_configured_origin() -> None:
    headers = await _preflight(["http://localhost:3000"])

    assert headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "idempotency-key" in headers["access-control-allow-headers"].lower()


async def test_cors_rejects_other_origins() -> None:
    headers = await _preflight(["https://admin.dhansetu.example"])

    assert "access-control-allow-origin" not in headers
