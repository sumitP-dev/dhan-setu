import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import (
    CardNetwork,
    CardStatus,
    CardType,
    CardVerificationStatus,
    RefundStatus,
)
from app.core.database import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin, str_enum

_ACTIVE = text("status = 'active'")


class Card(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A card that passed the ₹1 check. Only the gateway token and display details are
    kept: never the card number or CVV."""

    __tablename__ = "cards"
    __table_args__ = (
        CheckConstraint("expiry_month BETWEEN 1 AND 12", name="expiry_month_valid"),
        CheckConstraint("length(last4) = 4", name="last4_length"),
        # A card can be saved once per user, and each user has at most one default.
        Index(
            "uq_cards_user_token_active",
            "user_id",
            "gateway_token_id",
            unique=True,
            postgresql_where=_ACTIVE,
            sqlite_where=_ACTIVE,
        ),
        Index(
            "uq_cards_user_default",
            "user_id",
            unique=True,
            postgresql_where=text("is_default AND status = 'active'"),
            sqlite_where=text("is_default AND status = 'active'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    gateway_customer_id: Mapped[str] = mapped_column(String(64))
    gateway_token_id: Mapped[str] = mapped_column(String(64))
    last4: Mapped[str] = mapped_column(String(4))
    network: Mapped[CardNetwork] = mapped_column(str_enum(CardNetwork, length=16))
    card_type: Mapped[CardType] = mapped_column(str_enum(CardType, length=16))
    issuer: Mapped[str | None] = mapped_column(String(100))
    expiry_month: Mapped[int]
    expiry_year: Mapped[int]
    is_default: Mapped[bool] = mapped_column(default=False, server_default="false")
    status: Mapped[CardStatus] = mapped_column(
        str_enum(CardStatus, length=16), default=CardStatus.ACTIVE, server_default="active"
    )
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def __repr__(self) -> str:
        return f"Card(id={self.id!s}, last4={self.last4!r})"


def card_expired(expiry_month: int, expiry_year: int, now: datetime) -> bool:
    """A card works until the end of its expiry month."""
    return (expiry_year, expiry_month) < (now.year, now.month)


class CardVerification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One ₹1 check of a new card: the order, the payment, its refund and the result."""

    __tablename__ = "card_verifications"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_card_verifications_user_idempotency_key"
        ),
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        Index("ix_card_verifications_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    idempotency_key: Mapped[str] = mapped_column(String(64))
    gateway_customer_id: Mapped[str] = mapped_column(String(64))
    gateway_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    amount_paise: Mapped[int]
    status: Mapped[CardVerificationStatus] = mapped_column(
        str_enum(CardVerificationStatus, length=16),
        default=CardVerificationStatus.CREATED,
        server_default="created",
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    gateway_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    card_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cards.id", ondelete="RESTRICT"))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    refund_status: Mapped[RefundStatus | None] = mapped_column(str_enum(RefundStatus, length=16))
    gateway_refund_id: Mapped[str | None] = mapped_column(String(64), unique=True)

    def __repr__(self) -> str:
        return f"CardVerification(id={self.id!s}, status={self.status!s})"
