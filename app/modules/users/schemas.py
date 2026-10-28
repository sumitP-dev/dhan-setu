import uuid
from datetime import datetime

from app.common.schemas import RequestModel, ResponseModel
from app.common.validators import FullName


class UserRead(ResponseModel):
    id: uuid.UUID
    email: str
    phone: str
    full_name: str
    created_at: datetime


class UserUpdate(RequestModel):
    """Only the name can change for now. Email and phone changes need verification
    (OTP), which comes in a later phase."""

    full_name: FullName | None = None
