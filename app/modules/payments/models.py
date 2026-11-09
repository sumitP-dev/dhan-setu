import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import Currency, PaymentMethod, PaymentStatus, RefundStatus
from app.core.database import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin, str_enum


class GatewayCustomer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Links a user to their customer record at the gateway. Saved cards belong to it."""

    __tablename__ = "gateway_customers"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), unique=True
    )
    gateway_customer_id: Mapped[str] = mapped_column(String(64), unique=True)

    def __repr__(self) -> str:
        return f"GatewayCustomer(user_id={self.user_id!s})"


class Payment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One payment the user asked for. Each gets its own gateway order; the gateway
    payment that settled it is recorded once known."""

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_payments_user_idempotency_key"),
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        Index("ix_payments_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    # SHA-256 of the request body: the same key with a different body is refused.
    request_hash: Mapped[str] = mapped_column(String(64))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[Currency] = mapped_column(str_enum(Currency, length=3))
    method: Mapped[PaymentMethod] = mapped_column(str_enum(PaymentMethod, length=16))
    status: Mapped[PaymentStatus] = mapped_column(
        str_enum(PaymentStatus, length=16),
        default=PaymentStatus.CREATED,
        server_default="created",
    )
    description: Mapped[str | None] = mapped_column(String(100))
    card_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cards.id", ondelete="RESTRICT"), index=True
    )
    gateway_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    gateway_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Last time the status was checked with the gateway (limits polling).
    synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Only when money taken with a card that wasn't allowed has to go back.
    refund_status: Mapped[RefundStatus | None] = mapped_column(str_enum(RefundStatus, length=16))
    gateway_refund_id: Mapped[str | None] = mapped_column(String(64), unique=True)

    def __repr__(self) -> str:
        return f"Payment(id={self.id!s}, status={self.status!s})"


class PaymentEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Audit trail: one row per status change. Never updated or deleted."""

    __tablename__ = "payment_events"

    payment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), index=True
    )
    from_status: Mapped[PaymentStatus | None] = mapped_column(str_enum(PaymentStatus, length=16))
    to_status: Mapped[PaymentStatus] = mapped_column(str_enum(PaymentStatus, length=16))
    # Why it changed, e.g. "checkout_verified", "gateway_sync", "card_not_allowed".
    reason: Mapped[str] = mapped_column(String(64))
    gateway_payment_id: Mapped[str | None] = mapped_column(String(64))

    def __repr__(self) -> str:
        return f"PaymentEvent({self.from_status} -> {self.to_status})"
