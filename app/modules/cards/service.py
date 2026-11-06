"""Saved cards. A card is saved only after a ₹1 charge on it succeeds; the ₹1 is then
refunded. Only saved (active) cards can be used for payments.

Flow:
1. start_verification: makes a ₹1 gateway order for this user.
2. The app opens checkout with that order and save=1; the user pays ₹1.
3. complete_verification: checks the checkout signature, then asks the gateway what
   really happened. The card details come from the gateway, never from the app.
4. The card is saved, and the ₹1 refunded.

Once the ₹1 is taken, nothing here raises: every outcome is saved and returned, because
an error would roll back the transaction and lose the record of the charge.
"""

import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from app.common.enums import (
    CardStatus,
    CardType,
    CardVerificationStatus,
    Currency,
    RefundStatus,
)
from app.common.exceptions import (
    BadRequestError,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    TooManyRequestsError,
)
from app.core.config import Settings
from app.core.database import utcnow
from app.modules.cards.models import Card, CardVerification
from app.modules.cards.repository import CardRepository, CardVerificationRepository
from app.modules.payments.gateway import PaymentGateway
from app.modules.payments.gateway.base import (
    MIN_ORDER_AMOUNT_PAISE,
    GatewayNotFoundError,
    GatewayPayment,
    GatewayPaymentStatus,
    GatewayRefundStatus,
    PaymentGatewayError,
    PaymentGatewayUnavailableError,
)
from app.modules.payments.service import GatewayCustomerService
from app.modules.users.models import User

logger = logging.getLogger(__name__)

VERIFICATION_AMOUNT_PAISE = MIN_ORDER_AMOUNT_PAISE  # ₹1
VERIFICATION_TTL = timedelta(minutes=30)
SUPPORTED_CARD_TYPES = frozenset({CardType.CREDIT, CardType.DEBIT})

FAILURE_MESSAGES = {
    "payment_failed": "The ₹1 charge didn't go through. Try again or use another card.",
    "payment_refunded": "The ₹1 charge was reversed before the card could be checked. Try again.",
    "card_not_saved": "The card wasn't saved. Try again and keep 'Save card' switched on.",
    "card_type_not_supported": "Only credit and debit cards can be added.",
    "card_expired": "This card has expired.",
    "card_limit_reached": "You've reached the maximum number of saved cards.",
}


@dataclass(frozen=True, slots=True)
class VerificationResult:
    verification: CardVerification
    card: Card | None


class _CardRejectedError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def verification_not_found() -> NotFoundError:
    return NotFoundError("Card check not found", code="verification_not_found")


def card_not_found() -> NotFoundError:
    return NotFoundError("Card not found", code="card_not_found")


class CardService:
    def __init__(
        self,
        *,
        cards: CardRepository,
        verifications: CardVerificationRepository,
        customers: GatewayCustomerService,
        gateway: PaymentGateway,
        settings: Settings,
    ) -> None:
        self._cards = cards
        self._verifications = verifications
        self._customers = customers
        self._gateway = gateway
        self._settings = settings

    # ---- Cards

    async def list_cards(self, user: User) -> list[Card]:
        return await self._cards.list_active(user.id)

    async def set_default(self, user: User, card_id: uuid.UUID) -> Card:
        await self._customers.lock(user.id)
        card = await self._cards.get_active(user.id, card_id)
        if card is None:
            raise card_not_found()
        current = await self._cards.get_default(user.id)
        if current is not None and current.id != card.id:
            current.is_default = False
            await self._cards.flush()  # clear the old default first (one default per user)
        card.is_default = True
        await self._cards.flush()
        return card

    async def remove_card(self, user: User, card_id: uuid.UUID) -> None:
        await self._customers.lock(user.id)
        card = await self._cards.get_active(user.id, card_id)
        if card is None:
            raise card_not_found()
        # Delete the token at the gateway first: if that fails nothing changes here and the
        # user can try again, so a card never looks removed while it can still be charged.
        with contextlib.suppress(GatewayNotFoundError):  # already gone at the gateway
            await self._gateway.delete_card_token(card.gateway_customer_id, card.gateway_token_id)
        was_default = card.is_default
        card.status = CardStatus.REMOVED
        card.is_default = False
        card.removed_at = utcnow()
        await self._cards.flush()
        if was_default:
            replacement = await self._cards.newest_active(user.id)
            if replacement is not None:
                replacement.is_default = True
                await self._cards.flush()

    # ---- ₹1 card check

    async def start_verification(self, user: User, idempotency_key: str) -> CardVerification:
        existing = await self._verifications.get_by_key(user.id, idempotency_key)
        if existing is not None:
            return existing

        if await self._cards.count_active(user.id) >= self._settings.max_cards_per_user:
            raise InvalidInputError(
                FAILURE_MESSAGES["card_limit_reached"], code="card_limit_reached"
            )
        since = utcnow() - timedelta(days=1)
        if (
            await self._verifications.count_since(user.id, since)
            >= self._settings.card_verifications_per_day
        ):
            raise TooManyRequestsError(
                "Too many cards added today. Try again tomorrow.",
                code="card_verification_limit",
                retry_after_seconds=int(timedelta(hours=1).total_seconds()),
            )

        customer_id = await self._customers.get_or_create(user)
        verification_id = uuid.uuid4()
        order = await self._gateway.create_order(
            amount_paise=VERIFICATION_AMOUNT_PAISE,
            receipt=f"cv_{verification_id.hex}",
            notes={"purpose": "card_verification", "verification_id": str(verification_id)},
        )
        verification = CardVerification(
            id=verification_id,
            user_id=user.id,
            idempotency_key=idempotency_key,
            gateway_customer_id=customer_id,
            gateway_order_id=order.id,
            amount_paise=order.amount_paise,
            status=CardVerificationStatus.CREATED,
            expires_at=utcnow() + VERIFICATION_TTL,
        )
        if await self._verifications.add_if_absent(verification):
            return verification
        # Same key sent twice at once: the other request won. Our order is never paid.
        winner = await self._verifications.get_by_key(user.id, idempotency_key)
        if winner is None:
            raise ConflictError("Try again", code="conflict")
        return winner

    async def get_verification(self, user: User, verification_id: uuid.UUID) -> VerificationResult:
        verification = await self._verifications.get(user.id, verification_id)
        if verification is None:
            raise verification_not_found()
        return await self._result(verification)

    async def complete_verification(
        self, user: User, verification_id: uuid.UUID, *, payment_id: str, signature: str
    ) -> VerificationResult:
        verification = await self._verifications.get(user.id, verification_id, for_update=True)
        if verification is None:
            raise verification_not_found()

        if verification.status != CardVerificationStatus.CREATED:
            # Already settled (a retry, or the webhook got there first). Only the same
            # payment may ask again; a different one can't reuse this check.
            if verification.gateway_payment_id != payment_id:
                raise ConflictError(
                    "This card check is already finished", code="verification_finished"
                )
            if verification.status != CardVerificationStatus.CHARGED:
                return await self._result(verification)

        # Proves the payment came from checkout for *this* order; without it, someone
        # could submit any payment id they know of.
        if not self._gateway.verify_payment_signature(
            verification.gateway_order_id, payment_id, signature
        ):
            logger.warning("Bad checkout signature for card check %s", verification.id)
            raise BadRequestError("Payment could not be verified", code="invalid_signature")

        payment = await self._gateway.fetch_payment(payment_id)
        await self.settle(verification, payment)
        return await self._result(verification)

    async def settle(self, verification: CardVerification, payment: GatewayPayment) -> None:
        """Apply the gateway's view of the payment. The caller holds the row lock."""
        self._ensure_payment_matches(verification, payment)

        if verification.status == CardVerificationStatus.CREATED:
            match payment.status:
                case GatewayPaymentStatus.CREATED:
                    raise ConflictError(
                        "The payment isn't complete yet", code="payment_not_complete"
                    )
                case GatewayPaymentStatus.FAILED:
                    verification.gateway_payment_id = payment.id
                    self._finish(verification, CardVerificationStatus.FAILED, "payment_failed")
                    await self._verifications.flush()
                    return
                case GatewayPaymentStatus.REFUNDED:
                    verification.gateway_payment_id = payment.id
                    verification.refund_status = RefundStatus.PROCESSED
                    self._finish(verification, CardVerificationStatus.FAILED, "payment_refunded")
                    await self._verifications.flush()
                    return
                case GatewayPaymentStatus.AUTHORIZED:
                    # Raises if it fails; nothing is saved yet, so a retry starts over.
                    payment = await self._gateway.capture_payment(
                        payment.id, verification.amount_paise
                    )
                case GatewayPaymentStatus.CAPTURED:
                    pass

            # The ₹1 is ours now. From here on, save every outcome; never raise.
            verification.gateway_payment_id = payment.id
            verification.status = CardVerificationStatus.CHARGED
            await self._verifications.flush()

        await self._save_card_and_refund(verification, payment)

    # ---- internals

    def _ensure_payment_matches(
        self, verification: CardVerification, payment: GatewayPayment
    ) -> None:
        if (
            payment.order_id != verification.gateway_order_id
            or payment.amount_paise != verification.amount_paise
            or payment.currency != Currency.INR
            or payment.method != "card"
        ):
            logger.error(
                "Payment %s doesn't match card check %s (order, amount, currency or method)",
                payment.id,
                verification.id,
            )
            raise BadRequestError("Payment could not be verified", code="payment_mismatch")

    async def _save_card_and_refund(
        self, verification: CardVerification, payment: GatewayPayment
    ) -> None:
        try:
            card = await self._save_card(verification, payment)
        except PaymentGatewayUnavailableError:
            # Stays "charged": completing again (or the webhook) picks up from here.
            logger.warning("Gateway unavailable while saving card for check %s", verification.id)
            return
        except PaymentGatewayError:
            await self._discard_token(verification, payment)
            self._finish(verification, CardVerificationStatus.FAILED, "card_not_saved")
        except _CardRejectedError as rejected:
            await self._discard_token(verification, payment)
            self._finish(verification, CardVerificationStatus.FAILED, rejected.code)
        else:
            verification.card_id = card.id
            self._finish(verification, CardVerificationStatus.VERIFIED)
        await self._verifications.flush()
        await self._refund(verification)

    async def _save_card(self, verification: CardVerification, payment: GatewayPayment) -> Card:
        user_id = verification.user_id
        # Only a token saved for *this user's* gateway customer counts.
        if payment.customer_id != verification.gateway_customer_id or not payment.token_id:
            raise _CardRejectedError("card_not_saved")

        token = await self._gateway.fetch_card_token(
            verification.gateway_customer_id, payment.token_id
        )
        details = token.card
        if details.card_type not in SUPPORTED_CARD_TYPES:
            raise _CardRejectedError("card_type_not_supported")
        if details.expiry_month is None or details.expiry_year is None:
            raise _CardRejectedError("card_not_saved")
        today = utcnow()
        # A card works until the end of its expiry month.
        if (details.expiry_year, details.expiry_month) < (today.year, today.month):
            raise _CardRejectedError("card_expired")

        await self._customers.lock(user_id)
        existing = await self._cards.get_active_by_token(user_id, token.id)
        if existing is not None:
            return existing  # the same card checked again
        if await self._cards.count_active(user_id) >= self._settings.max_cards_per_user:
            raise _CardRejectedError("card_limit_reached")

        card = Card(
            user_id=user_id,
            gateway_customer_id=verification.gateway_customer_id,
            gateway_token_id=token.id,
            last4=details.last4,
            network=details.network,
            card_type=details.card_type,
            issuer=details.issuer,
            expiry_month=details.expiry_month,
            expiry_year=details.expiry_year,
            is_default=await self._cards.get_default(user_id) is None,
            status=CardStatus.ACTIVE,
        )
        if not await self._cards.add(card):
            raise _CardRejectedError("card_not_saved")
        return card

    async def _discard_token(self, verification: CardVerification, payment: GatewayPayment) -> None:
        """A rejected card must not stay saved at the gateway either."""
        token_id = payment.token_id
        if not token_id or payment.customer_id != verification.gateway_customer_id:
            return
        if await self._cards.get_active_by_token(verification.user_id, token_id) is not None:
            return  # an earlier check saved this same card; keep it
        try:
            await self._gateway.delete_card_token(verification.gateway_customer_id, token_id)
        except PaymentGatewayError:
            logger.warning("Could not delete rejected card token for check %s", verification.id)

    async def _refund(self, verification: CardVerification) -> None:
        if verification.refund_status is not None or verification.gateway_payment_id is None:
            return
        # Saved before calling the gateway: if the call fails or times out, the refund
        # stays "pending" for the reconciliation job, which checks the gateway before
        # retrying so the user is never refunded twice.
        verification.refund_status = RefundStatus.PENDING
        await self._verifications.flush()
        try:
            refund = await self._gateway.refund_payment(
                verification.gateway_payment_id,
                verification.amount_paise,
                receipt=f"cvr_{verification.id.hex}",
            )
        except PaymentGatewayError:
            logger.warning("Refund of card check %s not confirmed; left pending", verification.id)
            return
        verification.gateway_refund_id = refund.id
        verification.refund_status = {
            GatewayRefundStatus.PROCESSED: RefundStatus.PROCESSED,
            GatewayRefundStatus.PENDING: RefundStatus.REQUESTED,
            GatewayRefundStatus.FAILED: RefundStatus.FAILED,
        }[refund.status]
        if verification.refund_status == RefundStatus.FAILED:
            logger.error("Refund %s of card check %s failed", refund.id, verification.id)
        await self._verifications.flush()

    @staticmethod
    def _finish(
        verification: CardVerification,
        status: CardVerificationStatus,
        failure_code: str | None = None,
    ) -> None:
        verification.status = status
        verification.failure_code = failure_code
        verification.completed_at = utcnow()

    async def _result(self, verification: CardVerification) -> VerificationResult:
        card = None
        if verification.card_id is not None:
            card = await self._cards.get_by_id(verification.user_id, verification.card_id)
        return VerificationResult(verification, card)
