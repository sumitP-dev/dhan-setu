from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.common.schemas import ResponseModel


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class RazorpayEntity(_Lenient):
    id: str | None = None
    order_id: str | None = None
    payment_id: str | None = None


class RazorpayWrapped(_Lenient):
    entity: RazorpayEntity


class RazorpayPayload(_Lenient):
    payment: RazorpayWrapped | None = None
    order: RazorpayWrapped | None = None
    refund: RazorpayWrapped | None = None


class RazorpayEvent(_Lenient):
    """Only used to find which order or refund the event is about. Amounts and statuses
    in the payload are never trusted: the gateway is asked again."""

    event: str = Field(max_length=64)
    payload: RazorpayPayload


class WebhookAck(ResponseModel):
    status: Literal["processed", "ignored", "failed", "duplicate"]
