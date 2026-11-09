import uuid
from datetime import datetime

from pydantic import Field, StrictInt

from app.common.enums import CardNetwork, Currency, PaymentMethod, PaymentStatus, RefundStatus
from app.common.schemas import RequestModel, ResponseModel
from app.modules.payments.gateway.base import MIN_ORDER_AMOUNT_PAISE


class CheckoutOptions(ResponseModel):
    """Pass these to Razorpay checkout."""

    key_id: str
    order_id: str
    customer_id: str


class PaymentCreate(RequestModel):
    # StrictInt: 100.0 or "100" are refused, so amounts are never floats.
    amount_paise: StrictInt = Field(ge=MIN_ORDER_AMOUNT_PAISE, examples=[50000])
    card_id: uuid.UUID = Field(description="A saved card (GET /cards). No other card is accepted.")
    # No control characters (they'd end up in logs and statements).
    description: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$"
    )


class PaymentVerify(RequestModel):
    """What Razorpay checkout returns (razorpay_payment_id, razorpay_signature)."""

    payment_id: str = Field(pattern=r"^pay_[A-Za-z0-9]{8,40}$")
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class PaymentCard(ResponseModel):
    id: uuid.UUID
    last4: str
    network: CardNetwork


class PaymentRead(ResponseModel):
    id: uuid.UUID
    status: PaymentStatus = Field(
        description="pending = being processed: poll GET /payments/{id} until it changes."
    )
    amount_paise: int
    currency: Currency
    method: PaymentMethod
    description: str | None
    card: PaymentCard | None
    failure_code: str | None
    failure_message: str | None
    refund_status: RefundStatus | None
    created_at: datetime
    paid_at: datetime | None


class PaymentOrderCreated(ResponseModel):
    payment: PaymentRead
    checkout: CheckoutOptions
