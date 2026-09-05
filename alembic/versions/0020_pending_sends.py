"""Add pending_sends and pending_send_attachments -- the undo-send staging
table and its attachments, moved into outbox (content_id included) once
send_after passes uncancelled. No foreign key onto any PostIMAP-owned
table, consistent with every other MailVerdict-owned table; the foreign
key between the two tables added here is between two of our own.

Revision ID: 0020_pending_sends
Revises: 0019_pg_trgm
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020_pending_sends"
down_revision: str | None = "0019_pg_trgm"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create pending_sends and pending_send_attachments."""
    op.create_table(
        "pending_sends",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column("from_addr", sa.Text, nullable=True),
        sa.Column("to_addrs", postgresql.JSONB, nullable=True),
        sa.Column("cc_addrs", postgresql.JSONB, nullable=True),
        sa.Column("bcc_addrs", postgresql.JSONB, nullable=True),
        sa.Column("subject", sa.Text, nullable=True),
        sa.Column("body_text", sa.Text, nullable=True),
        sa.Column("body_html", sa.Text, nullable=True),
        sa.Column("in_reply_to", sa.Text, nullable=True),
        sa.Column("references", sa.ARRAY(sa.Text), nullable=True),
        sa.Column("replaces_message_id", sa.Uuid, nullable=True),
        sa.Column("send_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # Partial: only an uncancelled row is ever a candidate for the worker's
    # claim query, and a sent one is deleted rather than flagged, so a row
    # cancelled or already sent never needs to be found by send_after again.
    op.execute(
        "CREATE INDEX idx_pending_sends_due ON pending_sends (send_after) "
        "WHERE cancelled_at IS NULL"
    )

    op.create_table(
        "pending_send_attachments",
        sa.Column("id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "pending_send_id", sa.Uuid,
            sa.ForeignKey("pending_sends.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("filename", sa.Text, nullable=True),
        sa.Column("content_type", sa.Text, nullable=True),
        sa.Column("data", sa.LargeBinary, nullable=False),
        sa.Column("content_id", sa.Text, nullable=True),
    )
    op.create_index(
        "idx_pending_send_attachments_pending_send_id",
        "pending_send_attachments", ["pending_send_id"],
    )


def downgrade() -> None:
    """Drop pending_send_attachments and pending_sends."""
    op.drop_index(
        "idx_pending_send_attachments_pending_send_id",
        table_name="pending_send_attachments",
    )
    op.drop_table("pending_send_attachments")
    op.execute("DROP INDEX idx_pending_sends_due")
    op.drop_table("pending_sends")
