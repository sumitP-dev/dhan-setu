"""add otp codes, user role and status, refresh token device info

Revision ID: 5b1e7d2f9a40
Revises: c3a84e22b954
Create Date: 2026-10-31 12:00:00

users.is_active is replaced by users.status: existing active users become "active",
disabled ones "blocked". New sign-ups start as "pending_verification".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b1e7d2f9a40"
down_revision: str | None = "c3a84e22b954"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUM_ARGS = {"native_enum": False}


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "role",
            sa.Enum("customer", "merchant", "admin", name="userrole", length=16, **_ENUM_ARGS),
            server_default="customer",
            nullable=False,
        ),
    )
    # Added with default "active" so existing users stay usable; changed below.
    op.add_column(
        "users",
        sa.Column(
            "status",
            sa.Enum(
                "pending_verification",
                "active",
                "blocked",
                name="userstatus",
                length=32,
                **_ENUM_ARGS,
            ),
            server_default="active",
            nullable=False,
        ),
    )
    op.execute("UPDATE users SET status = 'blocked' WHERE is_active = false")
    op.alter_column("users", "status", server_default="pending_verification")
    op.drop_column("users", "is_active")
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("refresh_tokens", sa.Column("device_info", sa.String(length=200), nullable=True))

    op.create_table(
        "otp_codes",
        sa.Column("phone", sa.String(length=13), nullable=False),
        sa.Column(
            "purpose",
            sa.Enum(
                "verify_phone",
                "login",
                "reset_password",
                name="otppurpose",
                length=32,
                **_ENUM_ARGS,
            ),
            nullable=False,
        ),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_otp_codes")),
    )
    op.create_index("ix_otp_codes_phone_purpose", "otp_codes", ["phone", "purpose"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_otp_codes_phone_purpose", table_name="otp_codes")
    op.drop_table("otp_codes")
    op.drop_column("refresh_tokens", "device_info")
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.execute("UPDATE users SET is_active = false WHERE status = 'blocked'")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "status")
    op.drop_column("users", "role")
