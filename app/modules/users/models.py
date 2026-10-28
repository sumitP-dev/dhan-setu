from sqlalchemy import Boolean, String, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # Stored lower-cased (see NormalizedEmail) so uniqueness is case-insensitive.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    # E.164, e.g. +919876543210.
    phone: Mapped[str] = mapped_column(String(13), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    def __repr__(self) -> str:
        # Never include password_hash.
        return f"User(id={self.id!s}, email={self.email!r})"
