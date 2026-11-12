import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AdminAuditLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """What admins looked at in the portal: opening a user's record and searching for
    people. Never updated or deleted. (Opening decrypted KYC details is recorded
    separately in kyc_access_logs.)"""

    __tablename__ = "admin_audit_logs"

    admin_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    # "view_user", "search_users", "search_cards"
    action: Mapped[str] = mapped_column(String(32))
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    # The search text, for searches.
    detail: Mapped[str | None] = mapped_column(String(200))

    def __repr__(self) -> str:
        return f"AdminAuditLog(admin_id={self.admin_id!s}, action={self.action})"
