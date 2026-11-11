import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import KycStatus
from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum


class KycEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Audit trail: one row per KYC status change. Never updated or deleted."""

    __tablename__ = "kyc_events"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    from_status: Mapped[KycStatus] = mapped_column(str_enum(KycStatus, length=16))
    to_status: Mapped[KycStatus] = mapped_column(str_enum(KycStatus, length=16))
    # Why it changed, e.g. "digilocker_completed", "submitted", "approved".
    reason: Mapped[str] = mapped_column(String(64))
    # The admin who approved or rejected; None when the user or the system changed it.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    def __repr__(self) -> str:
        return f"KycEvent({self.from_status} -> {self.to_status})"
