"""Razorpay webhooks.

A webhook is only a nudge: "something happened to order X". We check the signature,
skip events we've already seen, then ask Razorpay for the current state and apply it with
the same code the app's verify and polling use. So a forged, replayed or out-of-order
webhook can't make a payment succeed or change an amount.

Responses tell Razorpay whether to retry: 2xx = done (even if we couldn't use the event),
5xx = try again later (Razorpay couldn't be reached, so nothing was saved).
"""

import hashlib
import json
import logging
import re

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import WebhookEventStatus
from app.common.exceptions import AppError, BadRequestError, InvalidInputError
from app.core.database import utcnow
from app.modules.cards.service import CardService
from app.modules.payments.gateway import PaymentGateway
from app.modules.payments.gateway.base import (
    GatewayNotFoundError,
    require_gateway_id,
)
from app.modules.payments.service import PaymentService
from app.modules.webhooks.models import WebhookEvent
from app.modules.webhooks.repository import WebhookEventRepository
from app.modules.webhooks.schemas import RazorpayEvent

logger = logging.getLogger(__name__)

PROVIDER = "razorpay"
PAYMENT_EVENTS = frozenset(
    {"payment.authorized", "payment.captured", "payment.failed", "order.paid"}
)
REFUND_EVENTS = frozenset({"refund.created", "refund.processed", "refund.failed"})
_EVENT_ID_RE = re.compile(r"[A-Za-z0-9_]{8,64}")


def bad_payload() -> InvalidInputError:
    return InvalidInputError("Unusable webhook payload", code="bad_payload")


class RazorpayWebhookService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        events: WebhookEventRepository,
        payments: PaymentService,
        cards: CardService,
        gateway: PaymentGateway,
    ) -> None:
        self._session = session
        self._events = events
        self._payments = payments
        self._cards = cards
        self._gateway = gateway

    async def handle(self, body: bytes, signature: str | None, event_id: str | None) -> str:
        if not self._gateway.verify_webhook_signature(body, signature):
            logger.warning("Rejected a Razorpay webhook with a bad signature")
            raise BadRequestError("Invalid signature", code="invalid_signature")

        parsed = self._parse(body)
        event = WebhookEvent(
            provider=PROVIDER,
            event_id=event_id if event_id and _EVENT_ID_RE.fullmatch(event_id) else None,
            payload_hash=hashlib.sha256(body).hexdigest(),
            event_type=parsed.event if parsed else "unparsable",
            status=WebhookEventStatus.FAILED,
        )
        if await self._events.seen(PROVIDER, event.event_id, event.payload_hash):
            logger.info("Duplicate Razorpay webhook %s ignored", event_id)
            return "duplicate"

        # Handle first, record after. If the gateway can't be reached the error goes back
        # to Razorpay with nothing recorded, so its retry is handled, not skipped as a
        # duplicate. Handling twice (two deliveries at once) is harmless: every step
        # re-reads the gateway under a row lock and changes nothing the second time.
        try:
            if parsed is None:
                raise bad_payload()
            # Savepoint: if handling fails half way, its changes are undone.
            async with self._session.begin_nested():
                event.status, event.reference = await self._dispatch(parsed)
        except GatewayNotFoundError:
            event.status, event.error = WebhookEventStatus.FAILED, "not_found_at_gateway"
        except AppError as exc:
            if exc.status_code >= 500:  # noqa: PLR2004
                raise  # gateway unreachable: Razorpay retries
            event.status, event.error = WebhookEventStatus.FAILED, exc.code
        if event.status == WebhookEventStatus.FAILED:
            logger.error(
                "Razorpay webhook %s (%s) failed: %s", event_id, event.event_type, event.error
            )
        event.processed_at = utcnow()
        if not await self._events.add_if_new(event):
            return "duplicate"  # the same event arrived twice at once; already handled
        return event.status.value

    @staticmethod
    def _parse(body: bytes) -> RazorpayEvent | None:
        try:
            return RazorpayEvent.model_validate(json.loads(body))
        except (ValueError, ValidationError):
            return None

    async def _dispatch(self, parsed: RazorpayEvent) -> tuple[WebhookEventStatus, str | None]:
        payload = parsed.payload
        if parsed.event in PAYMENT_EVENTS:
            order_id = (payload.payment and payload.payment.entity.order_id) or (
                payload.order and payload.order.entity.id
            )
            if not order_id:
                raise bad_payload()
            require_gateway_id(order_id, "order")
            handled = await self._payments.settle_order(order_id) is not None
            handled = handled or await self._cards.settle_order(order_id)
            return self._outcome(handled, order_id)

        if parsed.event in REFUND_EVENTS:
            entity = payload.refund.entity if payload.refund else None
            if entity is None or not entity.id or not entity.payment_id:
                raise bad_payload()
            require_gateway_id(entity.id, "rfnd")
            require_gateway_id(entity.payment_id, "pay")
            refund = await self._gateway.fetch_refund(entity.payment_id, entity.id)
            handled = await self._payments.apply_refund(refund)
            handled = handled or await self._cards.apply_refund(refund)
            return self._outcome(handled, entity.id)

        return WebhookEventStatus.IGNORED, None

    @staticmethod
    def _outcome(handled: bool, reference: str) -> tuple[WebhookEventStatus, str]:
        if not handled:
            logger.info("Razorpay webhook for %s, which isn't ours", reference)
        return (WebhookEventStatus.PROCESSED if handled else WebhookEventStatus.IGNORED), reference
