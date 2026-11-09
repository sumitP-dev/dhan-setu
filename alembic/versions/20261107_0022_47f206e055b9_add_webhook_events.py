"""add webhook events

Revision ID: 47f206e055b9
Revises: 4d172adfa650
Create Date: 2026-11-07 00:22:44

webhook_events: one row per accepted webhook, so each event is handled once.
Payloads aren't stored (they hold customer details).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "47f206e055b9"
down_revision: str | None = "4d172adfa650"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_events",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("reference", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "processed",
                "ignored",
                "failed",
                name="webhookeventstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("error", sa.String(length=64), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_events")),
        sa.UniqueConstraint("payload_hash", name=op.f("uq_webhook_events_payload_hash")),
        sa.UniqueConstraint("provider", "event_id", name="uq_webhook_events_event"),
    )


def downgrade() -> None:
    op.drop_table("webhook_events")
