"""add cards, card verifications and gateway customers

Revision ID: 048a8dbd2296
Revises: 5b1e7d2f9a40
Create Date: 2026-11-06 20:01:49

Cards are saved only after a ₹1 check (card_verifications). No card numbers or CVVs:
only the gateway token, last 4 digits, network and expiry.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "048a8dbd2296"
down_revision: str | None = "5b1e7d2f9a40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cards",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("gateway_customer_id", sa.String(length=64), nullable=False),
        sa.Column("gateway_token_id", sa.String(length=64), nullable=False),
        sa.Column("last4", sa.String(length=4), nullable=False),
        sa.Column(
            "network",
            sa.Enum(
                "visa",
                "mastercard",
                "rupay",
                "amex",
                "diners",
                "maestro",
                "jcb",
                "unknown",
                name="cardnetwork",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "card_type",
            sa.Enum(
                "credit",
                "debit",
                "prepaid",
                "unknown",
                name="cardtype",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(length=100), nullable=True),
        sa.Column("expiry_month", sa.Integer(), nullable=False),
        sa.Column("expiry_year", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "status",
            sa.Enum("active", "removed", name="cardstatus", native_enum=False, length=16),
            server_default="active",
            nullable=False,
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "expiry_month BETWEEN 1 AND 12", name=op.f("ck_cards_expiry_month_valid")
        ),
        sa.CheckConstraint("length(last4) = 4", name=op.f("ck_cards_last4_length")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_cards_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cards")),
    )
    op.create_index(op.f("ix_cards_user_id"), "cards", ["user_id"], unique=False)
    op.create_index(
        "uq_cards_user_default",
        "cards",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_default AND status = 'active'"),
        sqlite_where=sa.text("is_default AND status = 'active'"),
    )
    op.create_index(
        "uq_cards_user_token_active",
        "cards",
        ["user_id", "gateway_token_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "gateway_customers",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("gateway_customer_id", sa.String(length=64), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_gateway_customers_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gateway_customers")),
        sa.UniqueConstraint(
            "gateway_customer_id", name=op.f("uq_gateway_customers_gateway_customer_id")
        ),
        sa.UniqueConstraint("user_id", name=op.f("uq_gateway_customers_user_id")),
    )
    op.create_table(
        "card_verifications",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("gateway_customer_id", sa.String(length=64), nullable=False),
        sa.Column("gateway_order_id", sa.String(length=64), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "created",
                "charged",
                "verified",
                "failed",
                name="cardverificationstatus",
                native_enum=False,
                length=16,
            ),
            server_default="created",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gateway_payment_id", sa.String(length=64), nullable=True),
        sa.Column("card_id", sa.Uuid(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "refund_status",
            sa.Enum(
                "pending",
                "requested",
                "processed",
                "failed",
                name="refundstatus",
                native_enum=False,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("gateway_refund_id", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint("amount_paise > 0", name=op.f("ck_card_verifications_amount_positive")),
        sa.ForeignKeyConstraint(
            ["card_id"],
            ["cards.id"],
            name=op.f("fk_card_verifications_card_id_cards"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_card_verifications_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_card_verifications")),
        sa.UniqueConstraint(
            "gateway_order_id", name=op.f("uq_card_verifications_gateway_order_id")
        ),
        sa.UniqueConstraint(
            "gateway_payment_id", name=op.f("uq_card_verifications_gateway_payment_id")
        ),
        sa.UniqueConstraint(
            "gateway_refund_id", name=op.f("uq_card_verifications_gateway_refund_id")
        ),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name="uq_card_verifications_user_idempotency_key"
        ),
    )
    op.create_index(
        "ix_card_verifications_user_created",
        "card_verifications",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_card_verifications_user_created", table_name="card_verifications")
    op.drop_table("card_verifications")
    op.drop_table("gateway_customers")
    op.drop_index(
        "uq_cards_user_token_active",
        table_name="cards",
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.drop_index(
        "uq_cards_user_default",
        table_name="cards",
        postgresql_where=sa.text("is_default AND status = 'active'"),
        sqlite_where=sa.text("is_default AND status = 'active'"),
    )
    op.drop_index(op.f("ix_cards_user_id"), table_name="cards")
    op.drop_table("cards")
