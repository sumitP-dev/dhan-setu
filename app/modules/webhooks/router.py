from typing import Annotated

from fastapi import APIRouter, Header, Request, status

from app.common.exceptions import AppError
from app.common.schemas import error_responses
from app.modules.webhooks.dependencies import RazorpayWebhookServiceDep
from app.modules.webhooks.schemas import WebhookAck

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Razorpay events are a few KB. Anything far bigger isn't from Razorpay.
MAX_WEBHOOK_BYTES = 256 * 1024


class PayloadTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "payload_too_large"
    message = "Request body too large"


async def read_limited_body(request: Request) -> bytes:
    """The raw body (the signature covers these exact bytes), refusing oversized ones
    without reading them into memory."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
        raise PayloadTooLargeError
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_WEBHOOK_BYTES:
            raise PayloadTooLargeError
    return bytes(body)


@router.post(
    "/razorpay",
    responses=error_responses(
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_413_CONTENT_TOO_LARGE,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_503_SERVICE_UNAVAILABLE,
    ),
)
async def razorpay_webhook(
    request: Request,
    service: RazorpayWebhookServiceDep,
    x_razorpay_signature: Annotated[str | None, Header()] = None,
    x_razorpay_event_id: Annotated[str | None, Header()] = None,
) -> WebhookAck:
    """Called by Razorpay, not the app. No login: the X-Razorpay-Signature header (HMAC of
    the body with the webhook secret) proves it's from Razorpay."""
    body = await read_limited_body(request)
    outcome = await service.handle(body, x_razorpay_signature, x_razorpay_event_id)
    return WebhookAck(status=outcome)  # type: ignore[arg-type]
