from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.common.enums import KycStatus, UserRole, UserStatus
from app.core.database import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin, str_enum


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # Stored lower-cased (see NormalizedEmail) so uniqueness is case-insensitive.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    # E.164, e.g. +919876543210.
    phone: Mapped[str] = mapped_column(String(13), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        str_enum(UserRole, length=16), default=UserRole.CUSTOMER, server_default="customer"
    )
    # New accounts stay pending until the mobile number is verified by OTP.
    status: Mapped[UserStatus] = mapped_column(
        str_enum(UserStatus),
        default=UserStatus.PENDING_VERIFICATION,
        server_default="pending_verification",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Changed only by KycService, which records every change in kyc_events. Cards,
    # payments, UPI and bills need "verified".
    kyc_status: Mapped[KycStatus] = mapped_column(
        str_enum(KycStatus, length=16),
        default=KycStatus.NOT_SUBMITTED,
        server_default="not_submitted",
    )

    def __repr__(self) -> str:
        # Never include password_hash.
        return f"User(id={self.id!s}, email={self.email!r})"
