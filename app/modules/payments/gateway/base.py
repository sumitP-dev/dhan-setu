"""The payment gateway interface. RazorpayGateway talks to Razorpay; FakePaymentGateway
settles payments in memory for tests and local development.

Card numbers and CVVs never pass through here: the user types them into Razorpay's
checkout, and we only ever see Razorpay's ids, the last 4 digits and the card network.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from fastapi import status

from app.common.enums import CardNetwork, CardType, Currency
from app.common.exceptions import AppError, InvalidInputError

# Razorpay rejects orders below ₹1.
MIN_ORDER_AMOUNT_PAISE = 100
MAX_RECEIPT_LENGTH = 40
MAX_NOTES = 15
MAX_NOTE_LENGTH = 256


class PaymentGatewayError(AppError):
    """The gateway refused the request or answered with something unexpected."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "payment_gateway_error"
    message = "The payment provider could not process the request. Try again."


class PaymentGatewayUnavailableError(PaymentGatewayError):
    """Timeout, network failure or a 5xx from the gateway. The outcome is unknown."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "payment_gateway_unavailable"
    message = "The payment provider is not reachable. Try again in a moment."


class GatewayNotFoundError(PaymentGatewayError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "gateway_resource_not_found"
    message = "Payment not found"


class InvalidGatewayIdError(InvalidInputError):
    code = "invalid_gateway_id"
    message = "Invalid payment reference"


# Razorpay ids look like pay_29QQoUBi66xm2f. Checking the shape before an id goes into a
# URL path stops input like "../orders" from reaching a different endpoint.
_GATEWAY_ID_RE = re.compile(r"(order|pay|cust|token|rfnd)_[A-Za-z0-9]{8,40}")


def require_gateway_id(value: str, prefix: str) -> str:
    match = _GATEWAY_ID_RE.fullmatch(value)
    if match is None or match.group(1) != prefix:
        raise InvalidGatewayIdError
    return value


def validate_order_request(amount_paise: int, receipt: str, notes: dict[str, str]) -> None:
    # bool is a subclass of int; True must not become a ₹0.01 order.
    if type(amount_paise) is not int or amount_paise < MIN_ORDER_AMOUNT_PAISE:
        msg = f"amount_paise must be an integer of at least {MIN_ORDER_AMOUNT_PAISE}"
        raise ValueError(msg)
    if not receipt or len(receipt) > MAX_RECEIPT_LENGTH:
        msg = f"receipt must be 1-{MAX_RECEIPT_LENGTH} characters"
        raise ValueError(msg)
    if len(notes) > MAX_NOTES or any(len(v) > MAX_NOTE_LENGTH for v in notes.values()):
        msg = f"at most {MAX_NOTES} notes of {MAX_NOTE_LENGTH} characters"
        raise ValueError(msg)


class GatewayPaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"  # money held on the card, not yet taken
    CAPTURED = "captured"  # money taken
    REFUNDED = "refunded"
    FAILED = "failed"


class GatewayRefundStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GatewayOrder:
    id: str
    amount_paise: int
    currency: Currency
    receipt: str


@dataclass(frozen=True, slots=True)
class GatewayCard:
    last4: str
    network: CardNetwork
    card_type: CardType
    issuer: str | None = None
    # Only card tokens carry the expiry; card details on a payment don't.
    expiry_month: int | None = None
    expiry_year: int | None = None


@dataclass(frozen=True, slots=True)
class GatewayPayment:
    id: str
    order_id: str
    amount_paise: int
    currency: Currency
    status: GatewayPaymentStatus
    method: str
    card: GatewayCard | None = None
    customer_id: str | None = None
    # Set when the user chose to save the card at checkout.
    token_id: str | None = None
    error_code: str | None = None
    error_description: str | None = None


@dataclass(frozen=True, slots=True)
class GatewayRefund:
    id: str
    payment_id: str
    amount_paise: int
    status: GatewayRefundStatus


@dataclass(frozen=True, slots=True)
class GatewayCardToken:
    id: str
    customer_id: str
    card: GatewayCard


class PaymentGateway(Protocol):
    @property
    def key_id(self) -> str:
        """Public key id the app passes to checkout."""
        ...

    async def create_customer(self, *, name: str, email: str, phone: str) -> str:
        """Returns the customer id; asking again for the same person returns the same id."""
        ...

    async def create_order(
        self, *, amount_paise: int, receipt: str, notes: dict[str, str]
    ) -> GatewayOrder: ...

    async def fetch_payment(self, payment_id: str) -> GatewayPayment: ...

    async def fetch_order_payments(self, order_id: str) -> list[GatewayPayment]:
        """Every payment attempt on the order (a user can retry after a failure)."""
        ...

    async def capture_payment(self, payment_id: str, amount_paise: int) -> GatewayPayment: ...

    async def refund_payment(
        self, payment_id: str, amount_paise: int, *, receipt: str
    ) -> GatewayRefund:
        """Not retried here: on a timeout the refund may or may not exist."""
        ...

    async def fetch_refund(self, payment_id: str, refund_id: str) -> GatewayRefund: ...

    async def fetch_card_token(self, customer_id: str, token_id: str) -> GatewayCardToken: ...

    async def delete_card_token(self, customer_id: str, token_id: str) -> None: ...

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool: ...

    def verify_webhook_signature(self, body: bytes, signature: str | None) -> bool: ...

    async def aclose(self) -> None: ...
