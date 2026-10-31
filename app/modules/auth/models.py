import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import OtpPurpose
from app.core.database import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin, str_enum

MAX_DEVICE_INFO_LENGTH = 200


class RefreshToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per issued refresh token. Each use revokes the token and issues a new one
    in the same family (one family = one login on one device)."""

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    # SHA-256 hex of the token. The token itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The client's User-Agent, so a user could later see which devices are signed in.
    device_info: Mapped[str | None] = mapped_column(String(MAX_DEVICE_INFO_LENGTH))

    def __repr__(self) -> str:
        return f"RefreshToken(id={self.id!s}, user_id={self.user_id!s})"


class OtpCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A one-time code sent by SMS. Only an HMAC of the code is stored."""

    __tablename__ = "otp_codes"
    __table_args__ = (Index("ix_otp_codes_phone_purpose", "phone", "purpose"),)

    phone: Mapped[str] = mapped_column(String(13))
    purpose: Mapped[OtpPurpose] = mapped_column(str_enum(OtpPurpose))
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    # Set when the code is used or replaced by a newer one.
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def __repr__(self) -> str:
        # Never include code_hash.
        return f"OtpCode(id={self.id!s}, purpose={self.purpose!s})"
