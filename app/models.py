"""Import every module's models here so Alembic autogenerate can see them."""

from app.core.database import Base
from app.modules.auth.models import OtpCode, RefreshToken
from app.modules.users.models import User

__all__ = ["Base", "OtpCode", "RefreshToken", "User"]
