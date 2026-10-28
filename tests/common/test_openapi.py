from typing import Any

from fastapi import FastAPI, status
from pydantic import BaseModel

from app.common.schemas import error_responses

ERROR_SCHEMA_REF = "#/components/schemas/ErrorResponse"


def _error_responses(spec: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (f"{method.upper()} {path}", code, response)
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        for code, response in operation["responses"].items()
        if int(code) >= 400
    ]


def test_every_documented_error_uses_error_response(app: FastAPI) -> None:
    errors = _error_responses(app.openapi())

    assert errors
    for route, code, response in errors:
        schema = response["content"]["application/json"]["schema"]
        assert schema == {"$ref": ERROR_SCHEMA_REF}, f"{route} {code}"


def test_fastapi_default_validation_schema_is_not_documented(app: FastAPI) -> None:
    """Fails if a route takes input but forgot to declare 422 via error_responses()."""
    schemas = app.openapi()["components"]["schemas"]

    assert "HTTPValidationError" not in schemas
    assert schemas["ErrorResponse"]["required"] == ["error"]
    assert set(schemas["ErrorDetail"]["required"]) == {"code", "message"}


def test_every_route_documents_500(app: FastAPI) -> None:
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            assert "500" in operation["responses"], f"{method.upper()} {path}"


def test_readiness_documents_503(app: FastAPI) -> None:
    operation = app.openapi()["paths"]["/api/v1/health/ready"]["get"]

    assert "503" in operation["responses"]


def test_route_with_input_documents_our_422(app: FastAPI) -> None:
    class Payload(BaseModel):
        amount_paise: int

    @app.post(
        "/api/v1/_test/input",
        responses=error_responses(status.HTTP_422_UNPROCESSABLE_CONTENT),
    )
    async def take_input(payload: Payload) -> None:
        return None

    operation = app.openapi()["paths"]["/api/v1/_test/input"]["post"]

    assert operation["responses"]["422"]["content"]["application/json"]["schema"] == {
        "$ref": ERROR_SCHEMA_REF
    }
