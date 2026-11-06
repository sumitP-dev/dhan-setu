import uuid
from datetime import datetime

from pydantic import Field

from app.common.enums import (
    CardNetwork,
    CardType,
    CardVerificationStatus,
    Currency,
    RefundStatus,
)
from app.common.schemas import RequestModel, ResponseModel


class CardRead(ResponseModel):
    id: uuid.UUID
    last4: str = Field(examples=["4242"])
    network: CardNetwork
    card_type: CardType
    issuer: str | None
    expiry_month: int
    expiry_year: int
    is_default: bool
    created_at: datetime


class CheckoutOptions(ResponseModel):
    """Pass these to Razorpay checkout, with save: 1 so the card is saved."""

    key_id: str
    order_id: str
    customer_id: str


class CardVerificationStarted(ResponseModel):
    id: uuid.UUID
    status: CardVerificationStatus
    amount_paise: int = Field(description="Charged and refunded straight away.")
    currency: Currency = Currency.INR
    expires_at: datetime
    checkout: CheckoutOptions


class CardVerificationComplete(RequestModel):
    """What Razorpay checkout returns (razorpay_payment_id, razorpay_signature)."""

    payment_id: str = Field(pattern=r"^pay_[A-Za-z0-9]{8,40}$")
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


class CardVerificationRead(ResponseModel):
    id: uuid.UUID
    status: CardVerificationStatus = Field(
        description="charged = ₹1 taken but the card isn't saved yet: complete again shortly."
    )
    amount_paise: int
    failure_code: str | None
    failure_message: str | None
    refund_status: RefundStatus | None
    card: CardRead | None
    completed_at: datetime | None
