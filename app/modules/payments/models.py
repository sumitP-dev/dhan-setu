import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class GatewayCustomer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Links a user to their customer record at the gateway. Saved cards belong to it."""

    __tablename__ = "gateway_customers"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), unique=True
    )
    gateway_customer_id: Mapped[str] = mapped_column(String(64), unique=True)

    def __repr__(self) -> str:
        return f"GatewayCustomer(user_id={self.user_id!s})"
