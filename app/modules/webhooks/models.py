from datetime import datetime

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import WebhookEventStatus
from app.core.database import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin, str_enum


class WebhookEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per webhook delivery we accepted, so each event is handled once.

    The payload itself isn't stored (it holds the customer's email, phone and card
    details); only the event type and the id it refers to.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_webhook_events_event"),)

    provider: Mapped[str] = mapped_column(String(32))
    # The provider's event id (X-Razorpay-Event-Id). It isn't covered by the signature,
    # so the body hash below is the second guard against replays.
    event_id: Mapped[str | None] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64), unique=True)
    event_type: Mapped[str] = mapped_column(String(64))
    reference: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[WebhookEventStatus] = mapped_column(str_enum(WebhookEventStatus, length=16))
    error: Mapped[str | None] = mapped_column(String(64))
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def __repr__(self) -> str:
        return f"WebhookEvent(id={self.id!s}, type={self.event_type!r})"
