import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin

MAX_USER_AGENT_LENGTH = 200


class AdminSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One admin portal sign-in. Every admin access token names its session, so ending
    the session (logout, expiry, role removed) cuts off access at once. Kept after it
    ends, as a record of who signed in when and from where."""

    __tablename__ = "admin_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    # SHA-256 of the current refresh token (in the portal's httpOnly cookie).
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # The one before it: seeing it again means the cookie was copied, unless two tabs
    # refreshed at the same moment (see AdminAuthService.refresh).
    previous_refresh_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    rotated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    idle_expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # "logout", "reused_token", "not_admin", ...
    revoke_reason: Mapped[str | None] = mapped_column(String(32))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(MAX_USER_AGENT_LENGTH))

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.idle_expires_at and now < self.expires_at

    def __repr__(self) -> str:
        return f"AdminSession(id={self.id!s}, user_id={self.user_id!s})"
