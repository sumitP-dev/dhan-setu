"""In-memory gateway for tests and local development. No money moves.

It follows the same rules as RazorpayGateway (id checks, order limits, real HMAC
signatures with its own secrets), so code that works against it works against Razorpay.
complete_checkout() plays the part of the user paying in Razorpay's checkout.
Settings refuse PAYMENT_GATEWAY=fake in production.
"""

import secrets
import string
from dataclasses import dataclass, replace

from app.common.enums import CardNetwork, CardType, Currency
from app.modules.payments.gateway.base import (
    GatewayCard,
    GatewayCardToken,
    GatewayNotFoundError,
    GatewayOrder,
    GatewayPayment,
    GatewayPaymentStatus,
    GatewayRefund,
    GatewayRefundStatus,
    PaymentGatewayError,
    PaymentGatewayUnavailableError,
    require_gateway_id,
    validate_order_request,
)
from app.modules.payments.gateway.signatures import (
    hmac_sha256_hex,
    payment_signature,
    signature_matches,
)

FAKE_KEY_ID = "rzp_test_fakegateway"

DEFAULT_TEST_CARD = GatewayCard(
    last4="1111",
    network=CardNetwork.VISA,
    card_type=CardType.CREDIT,
    issuer="HDFC",
    expiry_month=12,
    expiry_year=2030,
)


def _new_id(prefix: str) -> str:
    alphabet = string.ascii_letters + string.digits
    return f"{prefix}_" + "".join(secrets.choice(alphabet) for _ in range(14))


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    """What Razorpay checkout hands back to the app."""

    order_id: str
    payment_id: str
    signature: str


class FakePaymentGateway:
    def __init__(self) -> None:
        # Fresh random secrets per process, so fake signatures can't be guessed.
        self._key_secret = secrets.token_hex(32)
        self._webhook_secret = secrets.token_hex(32)
        self.unavailable = False  # tests: simulate a network failure
        self.orders: dict[str, GatewayOrder] = {}
        self.payments: dict[str, GatewayPayment] = {}
        self.tokens: dict[str, GatewayCardToken] = {}
        self.refunds: dict[str, GatewayRefund] = {}
        self._customers: dict[tuple[str, str], str] = {}

    @property
    def key_id(self) -> str:
        return FAKE_KEY_ID

    async def aclose(self) -> None:
        return None

    def _check_available(self) -> None:
        if self.unavailable:
            raise PaymentGatewayUnavailableError

    # ---- PaymentGateway

    async def create_customer(self, *, name: str, email: str, phone: str) -> str:
        self._check_available()
        return self._customers.setdefault((email, phone), _new_id("cust"))

    async def create_order(
        self, *, amount_paise: int, receipt: str, notes: dict[str, str]
    ) -> GatewayOrder:
        self._check_available()
        validate_order_request(amount_paise, receipt, notes)
        order = GatewayOrder(
            id=_new_id("order"), amount_paise=amount_paise, currency=Currency.INR, receipt=receipt
        )
        self.orders[order.id] = order
        return order

    async def fetch_payment(self, payment_id: str) -> GatewayPayment:
        require_gateway_id(payment_id, "pay")
        self._check_available()
        payment = self.payments.get(payment_id)
        if payment is None:
            raise GatewayNotFoundError
        return payment

    async def capture_payment(self, payment_id: str, amount_paise: int) -> GatewayPayment:
        payment = await self.fetch_payment(payment_id)
        if (
            payment.status != GatewayPaymentStatus.AUTHORIZED
            or payment.amount_paise != amount_paise
        ):
            raise PaymentGatewayError
        captured = replace(payment, status=GatewayPaymentStatus.CAPTURED)
        self.payments[payment_id] = captured
        return captured

    async def refund_payment(
        self, payment_id: str, amount_paise: int, *, receipt: str
    ) -> GatewayRefund:
        validate_order_request(amount_paise, receipt, {})
        payment = await self.fetch_payment(payment_id)
        if payment.status != GatewayPaymentStatus.CAPTURED or amount_paise > payment.amount_paise:
            raise PaymentGatewayError
        refund = GatewayRefund(
            id=_new_id("rfnd"),
            payment_id=payment_id,
            amount_paise=amount_paise,
            status=GatewayRefundStatus.PROCESSED,
        )
        self.refunds[refund.id] = refund
        self.payments[payment_id] = replace(payment, status=GatewayPaymentStatus.REFUNDED)
        return refund

    async def fetch_card_token(self, customer_id: str, token_id: str) -> GatewayCardToken:
        require_gateway_id(customer_id, "cust")
        require_gateway_id(token_id, "token")
        self._check_available()
        token = self.tokens.get(token_id)
        if token is None or token.customer_id != customer_id:
            raise GatewayNotFoundError
        return token

    async def delete_card_token(self, customer_id: str, token_id: str) -> None:
        await self.fetch_card_token(customer_id, token_id)
        del self.tokens[token_id]

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        expected = payment_signature(self._key_secret, order_id, payment_id)
        return signature_matches(expected, signature)

    def verify_webhook_signature(self, body: bytes, signature: str | None) -> bool:
        return signature_matches(hmac_sha256_hex(self._webhook_secret, body), signature)

    # ---- Simulation (tests and local development only)

    def complete_checkout(
        self,
        order_id: str,
        *,
        status: GatewayPaymentStatus = GatewayPaymentStatus.CAPTURED,
        card: GatewayCard = DEFAULT_TEST_CARD,
        customer_id: str | None = None,
        save_card: bool = False,
    ) -> CheckoutResult:
        """Pretend the user paid for order_id in checkout."""
        order = self.orders.get(order_id)
        if order is None:
            raise GatewayNotFoundError
        token_id = None
        if save_card and customer_id and status != GatewayPaymentStatus.FAILED:
            token = GatewayCardToken(id=_new_id("token"), customer_id=customer_id, card=card)
            self.tokens[token.id] = token
            token_id = token.id
        failed = status == GatewayPaymentStatus.FAILED
        payment = GatewayPayment(
            id=_new_id("pay"),
            order_id=order.id,
            amount_paise=order.amount_paise,
            currency=order.currency,
            status=status,
            method="card",
            # Razorpay omits expiry on payment card details.
            card=replace(card, expiry_month=None, expiry_year=None),
            customer_id=customer_id,
            token_id=token_id,
            error_code="BAD_REQUEST_ERROR" if failed else None,
            error_description="Payment declined by the bank" if failed else None,
        )
        self.payments[payment.id] = payment
        return CheckoutResult(
            order_id=order.id,
            payment_id=payment.id,
            signature=payment_signature(self._key_secret, order.id, payment.id),
        )

    def sign_webhook(self, body: bytes) -> str:
        return hmac_sha256_hex(self._webhook_secret, body)
