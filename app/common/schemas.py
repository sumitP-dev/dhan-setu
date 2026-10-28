"""Schemas shared by every module."""

from http import HTTPStatus
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RequestModel(BaseModel):
    """Base for request bodies. Unknown fields are rejected, so clients can't sneak in
    fields like is_active."""

    model_config = ConfigDict(extra="forbid")


class ResponseModel(BaseModel):
    """Base for response bodies. Can be built straight from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable, machine-readable error code.", examples=["not_found"])
    message: str = Field(description="Human-readable explanation.", examples=["Card not found"])


class ErrorResponse(BaseModel):
    """Body of every error response. Built by the handlers in app.common.exceptions."""

    error: ErrorDetail


def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI entries for the given error statuses, for a router's or route's `responses=`.

    Routes that take input must list 422; otherwise FastAPI documents its own
    validation error format, which this API does not use.
    """
    return {
        code: {"model": ErrorResponse, "description": HTTPStatus(code).phrase}
        for code in status_codes
    }
