import uuid
from datetime import datetime

from app.common.enums import (
    CardNetwork,
    CardStatus,
    CardType,
    KycStatus,
    UserRole,
    UserStatus,
)
from app.common.schemas import ResponseModel


class Page[T](ResponseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


# ---- Users


class AdminUserItem(ResponseModel):
    id: uuid.UUID
    full_name: str
    email: str
    phone: str
    role: UserRole
    status: UserStatus
    kyc_status: KycStatus
    active_cards: int
    created_at: datetime
    last_login_at: datetime | None


class AdminKycSummary(ResponseModel):
    status: KycStatus
    submitted_at: datetime | None
    decided_at: datetime | None
    decided_by: str | None  # the admin's name
    # Set when the latest decision was a rejection.
    rejection_reason: str | None
    rejection_note: str | None


class AdminCardRead(ResponseModel):
    """A saved card. Only what is safe to show: never the gateway token."""

    id: uuid.UUID
    last4: str
    network: CardNetwork
    card_type: CardType
    issuer: str | None
    expiry_month: int
    expiry_year: int
    expired: bool
    is_default: bool
    status: CardStatus
    created_at: datetime
    removed_at: datetime | None


class AdminUserDetail(AdminUserItem):
    kyc: AdminKycSummary
    cards: list[AdminCardRead]  # active first, then removed; newest first within each


# ---- Cards


class AdminCardOwner(ResponseModel):
    id: uuid.UUID
    full_name: str
    phone: str
    kyc_status: KycStatus


class AdminCardItem(AdminCardRead):
    owner: AdminCardOwner
