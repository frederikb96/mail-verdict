"""Secrets and webhook deliveries.

`secrets` holds named, encrypted values a rule's webhook action references.
`webhook_deliveries` is the webhook queue's work table and the durable
record of what was delivered. Also admits the webhook_failed alert kind,
raised when a delivery gives up, widening ck_alerts_kind the same way
0041_glacier_conflict_alert did.

No foreign key onto any PostIMAP-owned table, consistent with every other
MailVerdict-owned table.

Revision ID: 0042_webhooks
Revises: 0041_glacier_conflict_alert
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0042_webhooks"
down_revision: str | None = "0041_glacier_conflict_alert"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "secrets",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("msg_key", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "origin IN ('live', 'backfill')", name="ck_webhook_deliveries_origin",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'done', 'skipped', 'failed')",
            name="ck_webhook_deliveries_status",
        ),
    )
    op.create_index(
        "uq_webhook_deliveries_mail", "webhook_deliveries", ["name", "account_id", "msg_key"],
        unique=True,
    )
    op.create_index(
        "ix_webhook_deliveries_claim", "webhook_deliveries", ["priority", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind", "alerts",
        "kind IN ('mail', 'reminder', 'outbox_stalled', 'glacier_conflict', 'webhook_failed')",
    )


def downgrade() -> None:
    """Drop every webhook_failed alert with the kind, then the tables."""
    op.execute("DELETE FROM alerts WHERE kind = 'webhook_failed'")
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind", "alerts",
        "kind IN ('mail', 'reminder', 'outbox_stalled', 'glacier_conflict')",
    )
    op.drop_index("ix_webhook_deliveries_claim", table_name="webhook_deliveries")
    op.drop_index("uq_webhook_deliveries_mail", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_table("secrets")
