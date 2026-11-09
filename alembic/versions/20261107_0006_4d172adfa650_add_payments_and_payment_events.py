"""add payments and payment events

Revision ID: 4d172adfa650
Revises: 048a8dbd2296
Create Date: 2026-11-07 00:06:38

Card payments with a saved card, and payment_events: one row per status change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4d172adfa650"
down_revision: str | None = "048a8dbd2296"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.Enum("INR", name="currency", native_enum=False), nullable=False),
        sa.Column(
            "method",
            sa.Enum("card", "upi", "bbps", name="paymentmethod", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "created",
                "pending",
                "success",
                "failed",
                "refunded",
                name="paymentstatus",
                native_enum=False,
                length=16,
            ),
            server_default="created",
            nullable=False,
        ),
        sa.Column("description", sa.String(length=100), nullable=True),
        sa.Column("card_id", sa.Uuid(), nullable=True),
        sa.Column("gateway_order_id", sa.String(length=64), nullable=False),
        sa.Column("gateway_payment_id", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("amount_paise > 0", name=op.f("ck_payments_amount_positive")),
        sa.ForeignKeyConstraint(
            ["card_id"], ["cards.id"], name=op.f("fk_payments_card_id_cards"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_payments_user_id_users"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint("gateway_order_id", name=op.f("uq_payments_gateway_order_id")),
        sa.UniqueConstraint("gateway_payment_id", name=op.f("uq_payments_gateway_payment_id")),
        sa.UniqueConstraint("gateway_refund_id", name=op.f("uq_payments_gateway_refund_id")),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_payments_user_idempotency_key"),
    )
    op.create_index(op.f("ix_payments_card_id"), "payments", ["card_id"], unique=False)
    op.create_index("ix_payments_user_created", "payments", ["user_id", "created_at"], unique=False)
    op.create_table(
        "payment_events",
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column(
            "from_status",
            sa.Enum(
                "created",
                "pending",
                "success",
                "failed",
                "refunded",
                name="paymentstatus",
                native_enum=False,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column(
            "to_status",
            sa.Enum(
                "created",
                "pending",
                "success",
                "failed",
                "refunded",
                name="paymentstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("gateway_payment_id", sa.String(length=64), nullable=True),
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
            ["payment_id"],
            ["payments.id"],
            name=op.f("fk_payment_events_payment_id_payments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_events")),
    )
    op.create_index(
        op.f("ix_payment_events_payment_id"), "payment_events", ["payment_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_payment_events_payment_id"), table_name="payment_events")
    op.drop_table("payment_events")
    op.drop_index("ix_payments_user_created", table_name="payments")
    op.drop_index(op.f("ix_payments_card_id"), table_name="payments")
    op.drop_table("payments")
