import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from app.common.enums import Currency, PaymentMethod, PaymentStatus, RefundStatus
from app.common.exceptions import (
    BadRequestError,
    ConflictError,
    InvalidInputError,
    NotFoundError,
)
from app.core.config import Settings
from app.core.database import utcnow
from app.modules.cards.models import Card, CardVerification, card_expired
from app.modules.cards.repository import CardRepository
from app.modules.payments.gateway import PaymentGateway
from app.modules.payments.gateway.base import (
    GatewayPayment,
    GatewayPaymentStatus,
    GatewayRefund,
    GatewayRefundStatus,
    PaymentGatewayError,
)
from app.modules.payments.models import GatewayCustomer, Payment, PaymentEvent
from app.modules.payments.repository import GatewayCustomerRepository, PaymentRepository
from app.modules.payments.state import ensure_transition
from app.modules.users.models import User

logger = logging.getLogger(__name__)


class GatewayCustomerService:
    def __init__(self, *, customers: GatewayCustomerRepository, gateway: PaymentGateway) -> None:
        self._customers = customers
        self._gateway = gateway

    async def get_or_create(self, user: User) -> str:
        """The user's gateway customer id, created on first use."""
        existing = await self._customers.get_for_user(user.id)
        if existing is not None:
            return existing.gateway_customer_id

        # The gateway returns the same customer when asked twice, so a race between two
        # requests ends with both holding the same id; only one row is inserted.
        customer_id = await self._gateway.create_customer(
            name=user.full_name, email=user.email, phone=user.phone
        )
        if await self._customers.add_if_absent(
            GatewayCustomer(user_id=user.id, gateway_customer_id=customer_id)
        ):
            return customer_id

        existing = await self._customers.get_for_user(user.id)
        if existing is None or existing.gateway_customer_id != customer_id:
            # The gateway matched this user to a customer that belongs to someone else.
            logger.error("Gateway customer for user %s is already linked elsewhere", user.id)
            raise ConflictError(
                "We couldn't set up card payments for this account. Contact support.",
                code="gateway_customer_conflict",
            )
        return customer_id

    async def lock(self, user_id: uuid.UUID) -> None:
        """Lock the user's customer row until the transaction ends, so card changes for
        one user (save, set default, remove) happen one at a time."""
        await self._customers.get_for_user(user_id, for_update=True)


# ---- Payments

PAYMENT_FAILURE_MESSAGES = {
    "payment_failed": "The payment didn't go through. No money was taken.",
    "card_not_allowed": ("Pay with the saved card you picked. Any amount taken has been refunded."),
    "payment_expired": "The payment wasn't completed in time.",
}

# Polling stops checking with the gateway after this; webhooks still update the payment.
SYNC_WINDOW = timedelta(days=1)
_UNSETTLED = frozenset({PaymentStatus.CREATED, PaymentStatus.PENDING, PaymentStatus.FAILED})
# Which attempt on an order decides the payment, most decisive first.
_ATTEMPT_PRIORITY = (
    GatewayPaymentStatus.CAPTURED,
    GatewayPaymentStatus.AUTHORIZED,
    GatewayPaymentStatus.REFUNDED,
    GatewayPaymentStatus.FAILED,
)


def refund_status_from_gateway(status: GatewayRefundStatus) -> RefundStatus:
    return {
        GatewayRefundStatus.PROCESSED: RefundStatus.PROCESSED,
        GatewayRefundStatus.PENDING: RefundStatus.REQUESTED,
        GatewayRefundStatus.FAILED: RefundStatus.FAILED,
    }[status]


def apply_refund_update(row: "Payment | CardVerification", refund: GatewayRefund) -> None:
    """Record what the gateway says about a refund we asked for. Also adopts a refund
    whose request timed out (refund id unknown). Refunds we didn't ask for are left to
    the payment's own status sync."""
    if row.refund_status is None or row.amount_paise != refund.amount_paise:
        return
    if row.gateway_refund_id not in {None, refund.id}:
        logger.error("Refund %s isn't the one recorded for %s", refund.id, row.id)
        return
    row.gateway_refund_id = refund.id
    row.refund_status = refund_status_from_gateway(refund.status)
    if row.refund_status == RefundStatus.FAILED:
        logger.error("Refund %s for %s failed", refund.id, row.id)


@dataclass(frozen=True, slots=True)
class NewPayment:
    amount_paise: int
    card_id: uuid.UUID
    description: str | None

    def request_hash(self) -> str:
        canonical = json.dumps(
            {
                "amount_paise": self.amount_paise,
                "card_id": str(self.card_id),
                "description": self.description,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class PaymentResult:
    payment: Payment
    card: Card | None


def payment_not_found() -> NotFoundError:
    return NotFoundError("Payment not found", code="payment_not_found")


class PaymentService:
    """Card payments with a saved card.

    1. create_payment: a gateway order for the amount, tied to one saved card.
    2. The app opens checkout; the user pays.
    3. verify_payment: checks the checkout signature, then asks the gateway what really
       happened. Polling (get_payment) and webhooks reach the same result through
       settle_order, so whichever arrives first wins and the rest change nothing.

    The money is only accepted if it was paid with the chosen saved card; a payment with
    any other card is never captured, or is refunded if it already was.
    Once money may have moved, outcomes are saved and returned, never raised.
    """

    def __init__(
        self,
        *,
        payments: PaymentRepository,
        cards: CardRepository,
        gateway: PaymentGateway,
        settings: Settings,
    ) -> None:
        self._payments = payments
        self._cards = cards
        self._gateway = gateway
        self._settings = settings

    # ---- API

    async def create_payment(
        self, user: User, idempotency_key: str, request: NewPayment
    ) -> PaymentResult:
        request_hash = request.request_hash()
        existing = await self._payments.get_by_key(user.id, idempotency_key)
        if existing is not None:
            return await self._replay(existing, request_hash)

        if request.amount_paise > self._settings.max_payment_amount_paise:
            raise InvalidInputError(
                "This amount is above the limit for a single payment.", code="amount_too_large"
            )
        card = await self._cards.get_active(user.id, request.card_id)
        if card is None:
            raise NotFoundError("Card not found", code="card_not_found")
        if card_expired(card.expiry_month, card.expiry_year, utcnow()):
            raise InvalidInputError("This card has expired.", code="card_expired")

        payment_id = uuid.uuid4()
        order = await self._gateway.create_order(
            amount_paise=request.amount_paise,
            receipt=f"p_{payment_id.hex}",
            notes={"purpose": "payment", "payment_id": str(payment_id)},
        )
        payment = Payment(
            id=payment_id,
            user_id=user.id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            amount_paise=order.amount_paise,
            currency=Currency.INR,
            method=PaymentMethod.CARD,
            status=PaymentStatus.CREATED,
            description=request.description,
            card_id=card.id,
            gateway_order_id=order.id,
        )
        if not await self._payments.add_if_absent(payment):
            # Same key sent twice at once: the other request won. Our order is never paid.
            winner = await self._payments.get_by_key(user.id, idempotency_key)
            if winner is None:
                raise ConflictError("Try again", code="conflict")
            return await self._replay(winner, request_hash)
        self._record(payment, None, PaymentStatus.CREATED, "order_created")
        await self._payments.flush()
        return PaymentResult(payment, card)

    async def verify_payment(
        self, user: User, payment_id: uuid.UUID, *, gateway_payment_id: str, signature: str
    ) -> PaymentResult:
        payment = await self._payments.get(user.id, payment_id, for_update=True)
        if payment is None:
            raise payment_not_found()
        if payment.status in {PaymentStatus.SUCCESS, PaymentStatus.REFUNDED}:
            if payment.gateway_payment_id != gateway_payment_id:
                raise ConflictError(
                    "This payment is already complete", code="payment_already_completed"
                )
            return await self._result(payment)

        # Proves the payment came from checkout for *this* order.
        if not self._gateway.verify_payment_signature(
            payment.gateway_order_id, gateway_payment_id, signature
        ):
            logger.warning("Bad checkout signature for payment %s", payment.id)
            raise BadRequestError("Payment could not be verified", code="invalid_signature")

        attempt = await self._gateway.fetch_payment(gateway_payment_id)
        if (
            attempt.order_id != payment.gateway_order_id
            or attempt.amount_paise != payment.amount_paise
            or attempt.currency != payment.currency
        ):
            logger.error("Gateway payment %s doesn't match payment %s", attempt.id, payment.id)
            raise BadRequestError("Payment could not be verified", code="payment_mismatch")

        await self._apply(payment, attempt, reason="checkout_verified")
        payment.synced_at = utcnow()
        await self._payments.flush()
        return await self._result(payment)

    async def get_payment(self, user: User, payment_id: uuid.UUID) -> PaymentResult:
        """Polling. While the payment is unsettled this also checks with the gateway, at
        most once per sync interval; if the gateway can't be reached the saved state is
        returned."""
        payment = await self._payments.get(user.id, payment_id)
        if payment is None:
            raise payment_not_found()
        if self._needs_sync(payment):
            locked = await self._payments.get(user.id, payment_id, for_update=True)
            if locked is not None:
                payment = locked
                if self._needs_sync(payment):
                    try:
                        await self._sync(payment)
                    except PaymentGatewayError:
                        logger.warning("Couldn't check payment %s with the gateway", payment.id)
        return await self._result(payment)

    async def settle_order(self, order_id: str) -> Payment | None:
        """Bring a payment up to date from the gateway by its order (webhooks).
        None if the order isn't one of our payments."""
        payment = await self._payments.get_by_order_id(order_id, for_update=True)
        if payment is None:
            return None
        await self._sync(payment)
        return payment

    async def apply_refund(self, refund: GatewayRefund) -> bool:
        """Webhook: a refund changed. False if it isn't for one of our payments."""
        payment = await self._payments.get_by_gateway_payment_id(refund.payment_id, for_update=True)
        if payment is None:
            return False
        apply_refund_update(payment, refund)
        if payment.status == PaymentStatus.SUCCESS:
            await self._sync(payment)  # a refund made outside the app (dashboard)
        await self._payments.flush()
        return True

    # ---- Settling

    def _needs_sync(self, payment: Payment) -> bool:
        now = utcnow()
        if payment.status not in _UNSETTLED or payment.created_at < now - SYNC_WINDOW:
            return False
        interval = timedelta(seconds=self._settings.payment_sync_interval_seconds)
        return payment.synced_at is None or payment.synced_at <= now - interval

    async def _sync(self, payment: Payment) -> None:
        attempts = await self._gateway.fetch_order_payments(payment.gateway_order_id)
        payment.synced_at = utcnow()
        decisive = next(
            (a for status in _ATTEMPT_PRIORITY for a in attempts if a.status == status), None
        )
        if decisive is not None:
            if decisive.amount_paise != payment.amount_paise:
                logger.error("Gateway payment %s amount differs from %s", decisive.id, payment.id)
            else:
                await self._apply(payment, decisive, reason="gateway_sync")
        elif payment.status == PaymentStatus.CREATED and payment.created_at < utcnow() - timedelta(
            minutes=self._settings.payment_expiry_minutes
        ):
            self._move(payment, PaymentStatus.FAILED, "payment_expired")
            payment.failure_code = "payment_expired"
        await self._payments.flush()

    async def _apply(self, payment: Payment, attempt: GatewayPayment, *, reason: str) -> None:
        """Apply one gateway payment attempt. The caller holds the row lock."""
        match attempt.status:
            case GatewayPaymentStatus.CREATED:
                return  # the user hasn't finished paying
            case GatewayPaymentStatus.FAILED:
                await self._apply_failed(payment, attempt, reason)
            case GatewayPaymentStatus.AUTHORIZED:
                await self._apply_authorized(payment, attempt, reason)
            case GatewayPaymentStatus.CAPTURED:
                await self._apply_captured(payment, attempt, reason)
            case GatewayPaymentStatus.REFUNDED:
                if (
                    payment.status == PaymentStatus.SUCCESS
                    and payment.gateway_payment_id == attempt.id
                ):
                    self._move(payment, PaymentStatus.REFUNDED, "refunded_at_gateway", attempt.id)

    async def _apply_failed(self, payment: Payment, attempt: GatewayPayment, reason: str) -> None:
        # A failed attempt only fails the payment if no other attempt is in progress.
        if payment.status == PaymentStatus.CREATED or (
            payment.status == PaymentStatus.PENDING
            and payment.gateway_payment_id in {None, attempt.id}
        ):
            self._move(payment, PaymentStatus.FAILED, reason, attempt.id)
            payment.failure_code = "payment_failed"

    async def _apply_authorized(
        self, payment: Payment, attempt: GatewayPayment, reason: str
    ) -> None:
        if payment.status in {PaymentStatus.SUCCESS, PaymentStatus.REFUNDED}:
            logger.error("Second payment %s on settled payment %s", attempt.id, payment.id)
            return
        if not await self._paid_with_chosen_card(payment, attempt):
            # Not captured: the gateway releases the hold on its own. No money is taken.
            self._fail_wrong_card(payment, attempt)
            return
        self._move(payment, PaymentStatus.PENDING, reason, attempt.id)
        payment.gateway_payment_id = attempt.id
        await self._payments.flush()
        try:
            captured = await self._gateway.capture_payment(attempt.id, payment.amount_paise)
        except PaymentGatewayError:
            # Stays pending; polling or the webhook finishes it.
            logger.warning("Capture of %s for payment %s not confirmed", attempt.id, payment.id)
            return
        await self._apply_captured(payment, captured, reason)

    async def _apply_captured(self, payment: Payment, attempt: GatewayPayment, reason: str) -> None:
        if payment.status in {PaymentStatus.SUCCESS, PaymentStatus.REFUNDED}:
            if payment.gateway_payment_id != attempt.id:
                logger.error("Second captured payment %s on %s", attempt.id, payment.id)
            return
        if not await self._paid_with_chosen_card(payment, attempt):
            self._fail_wrong_card(payment, attempt)
            payment.gateway_payment_id = attempt.id
            await self._payments.flush()
            await self._refund(payment, attempt.id)
            return
        self._move(payment, PaymentStatus.PENDING, reason, attempt.id)
        self._move(payment, PaymentStatus.SUCCESS, reason, attempt.id)
        payment.gateway_payment_id = attempt.id
        payment.failure_code = None
        payment.paid_at = utcnow()

    async def _paid_with_chosen_card(self, payment: Payment, attempt: GatewayPayment) -> bool:
        if payment.card_id is None or attempt.method != "card" or not attempt.token_id:
            return False
        card = await self._cards.get_by_id(payment.user_id, payment.card_id)
        return (
            card is not None
            and attempt.token_id == card.gateway_token_id
            and attempt.customer_id == card.gateway_customer_id
        )

    def _fail_wrong_card(self, payment: Payment, attempt: GatewayPayment) -> None:
        logger.warning("Payment %s was paid with a card that wasn't chosen", payment.id)
        if payment.status != PaymentStatus.FAILED:
            self._move(payment, PaymentStatus.FAILED, "card_not_allowed", attempt.id)
        payment.failure_code = "card_not_allowed"

    async def _refund(self, payment: Payment, gateway_payment_id: str) -> None:
        if payment.refund_status is not None:
            return
        # Saved before calling the gateway; if it fails it stays pending for the
        # reconciliation job, which checks the gateway first so nobody is refunded twice.
        payment.refund_status = RefundStatus.PENDING
        await self._payments.flush()
        try:
            refund = await self._gateway.refund_payment(
                gateway_payment_id, payment.amount_paise, receipt=f"pr_{payment.id.hex}"
            )
        except PaymentGatewayError:
            logger.warning("Refund for payment %s not confirmed; left pending", payment.id)
            return
        payment.gateway_refund_id = refund.id
        payment.refund_status = refund_status_from_gateway(refund.status)
        if payment.refund_status == RefundStatus.FAILED:
            logger.error("Refund %s for payment %s failed", refund.id, payment.id)

    # ---- helpers

    def _move(
        self,
        payment: Payment,
        target: PaymentStatus,
        reason: str,
        gateway_payment_id: str | None = None,
    ) -> None:
        """Change status by the rules in state.py and record it. Same status: no-op."""
        if payment.status == target:
            return
        ensure_transition(payment.status, target)
        self._record(payment, payment.status, target, reason, gateway_payment_id)
        payment.status = target

    def _record(
        self,
        payment: Payment,
        from_status: PaymentStatus | None,
        to_status: PaymentStatus,
        reason: str,
        gateway_payment_id: str | None = None,
    ) -> None:
        self._payments.add_event(
            PaymentEvent(
                payment_id=payment.id,
                from_status=from_status,
                to_status=to_status,
                reason=reason,
                gateway_payment_id=gateway_payment_id,
            )
        )

    async def _replay(self, payment: Payment, request_hash: str) -> PaymentResult:
        if payment.request_hash != request_hash:
            raise ConflictError(
                "This Idempotency-Key was already used for a different payment",
                code="idempotency_key_reused",
            )
        return await self._result(payment)

    async def _result(self, payment: Payment) -> PaymentResult:
        card = None
        if payment.card_id is not None:
            card = await self._cards.get_by_id(payment.user_id, payment.card_id)
        return PaymentResult(payment, card)
