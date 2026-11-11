import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, SmallInteger, String, Text, false
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import KycStatus
from app.core.database import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin, str_enum
from app.modules.kyc.provider.base import DigiLockerStatus, Gender


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


class KycDigiLockerSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One DigiLocker consent session. The user is sent to the provider and comes back
    to our callback with a one-time `state` token; only its SHA-256 is stored."""

    __tablename__ = "kyc_digilocker_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    provider_session_id: Mapped[str] = mapped_column(String(64), unique=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Handed to the app again if it asks to start while this session is still open.
    consent_url: Mapped[str] = mapped_column(String(512))
    status: Mapped[DigiLockerStatus] = mapped_column(
        str_enum(DigiLockerStatus, length=16), default=DigiLockerStatus.PENDING
    )
    failure_reason: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def __repr__(self) -> str:
        return f"KycDigiLockerSession(id={self.id!s}, status={self.status})"


class KycProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The identity details from the latest completed DigiLocker session, one row per
    user. Name, date of birth, address and PAN are encrypted (app.core.encryption),
    bound to this user and field. The full Aadhaar number is never stored."""

    __tablename__ = "kyc_profiles"
    __table_args__ = (
        CheckConstraint("length(aadhaar_last4) = 4", name="aadhaar_last4_length"),
        CheckConstraint(
            "pan_source IS NULL OR pan_source IN ('digilocker', 'manual')", name="pan_source"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), unique=True
    )
    digilocker_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kyc_digilocker_sessions.id", ondelete="RESTRICT")
    )

    # Aadhaar
    name_encrypted: Mapped[str] = mapped_column(Text)
    date_of_birth_encrypted: Mapped[str | None] = mapped_column(Text)
    birth_year: Mapped[int] = mapped_column(SmallInteger)
    gender: Mapped[Gender] = mapped_column(str_enum(Gender, length=1))
    address_encrypted: Mapped[str | None] = mapped_column(Text)
    aadhaar_last4: Mapped[str] = mapped_column(String(4))

    # PAN
    pan_encrypted: Mapped[str | None] = mapped_column(Text)
    pan_masked: Mapped[str | None] = mapped_column(String(10))
    # Keyed hash of the PAN, so one PAN can't be used by two accounts.
    pan_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    pan_source: Mapped[str | None] = mapped_column(String(16))
    pan_verified: Mapped[bool] = mapped_column(default=False, server_default=false())
    # Why the PAN isn't verified, e.g. "pan_name_mismatch", "pan_already_used".
    pan_issue: Mapped[str | None] = mapped_column(String(64))
    # PANs typed in by hand; capped so the check can't be used to look up PANs.
    pan_attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")

    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)

    def __repr__(self) -> str:
        # Never include personal details.
        return f"KycProfile(user_id={self.user_id!s})"
