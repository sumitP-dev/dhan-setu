import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import BaseModel

from app.common.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    TooManyRequestsError,
)


class Payload(BaseModel):
    amount_paise: int


@pytest.fixture(autouse=True)
def _error_routes(app: FastAPI) -> None:
    """Routes that exist only in these tests, one per error path."""

    @app.post("/api/v1/_test/validate")
    async def validate(payload: Payload) -> dict[str, int]:
        return {"amount_paise": payload.amount_paise}

    @app.get("/api/v1/_test/not-found")
    async def not_found() -> None:
        raise NotFoundError("Card not found")

    @app.get("/api/v1/_test/conflict")
    async def conflict() -> None:
        raise ConflictError(code="duplicate_payment", message="Already processed")

    @app.get("/api/v1/_test/bad-request")
    async def bad_request() -> None:
        raise BadRequestError("Amount must be positive")

    @app.get("/api/v1/_test/rate-limited")
    async def rate_limited() -> None:
        raise TooManyRequestsError(retry_after_seconds=30)

    @app.get("/api/v1/_test/crash")
    async def crash() -> None:
        raise RuntimeError("secret internal detail")


async def test_app_error_shape(client: AsyncClient) -> None:
    response = await client.get("/api/v1/_test/not-found")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Card not found"}}


async def test_app_error_custom_code(client: AsyncClient) -> None:
    response = await client.get("/api/v1/_test/conflict")

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "duplicate_payment", "message": "Already processed"}
    }


async def test_validation_error_shape(client: AsyncClient) -> None:
    response = await client.post("/api/v1/_test/validate", json={})

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "validation_error", "message": "body.amount_paise: Field required"}
    }


async def test_unknown_route_shape(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


async def test_method_not_allowed_keeps_allow_header(client: AsyncClient) -> None:
    response = await client.post("/api/v1/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert response.headers["allow"] == "GET"


async def test_unhandled_error_hides_details(client: AsyncClient) -> None:
    response = await client.get("/api/v1/_test/crash")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Something went wrong"}
    }


async def test_bad_request_shape(client: AsyncClient) -> None:
    response = await client.get("/api/v1/_test/bad-request")

    assert response.status_code == 400
    assert response.json() == {
        "error": {"code": "bad_request", "message": "Amount must be positive"}
    }


async def test_too_many_requests_sets_retry_after(client: AsyncClient) -> None:
    response = await client.get("/api/v1/_test/rate-limited")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    assert response.json()["error"]["code"] == "too_many_requests"
