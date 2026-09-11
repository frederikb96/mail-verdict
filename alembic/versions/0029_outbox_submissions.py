"""Record every accepted POST /outbox under the caller's idempotency key,
and let an alert say a message is stuck on its way out.

A repeat of the same request is answered with the row the first one
created rather than creating a second message -- see OutboxSubmission in
database/models.py. The alerts kind check gains "outbox_stalled", the
alert outbox/stalled.py raises for a message waiting far longer than it
should.

Revision ID: 0029_outbox_submissions
Revises: 0028_retention_min_days
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0029_outbox_submissions"
down_revision: str | None = "0028_retention_min_days"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create outbox_submissions and admit the outbox_stalled alert kind."""
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint(
        "ck_alerts_kind", "alerts", "kind IN ('mail', 'reminder', 'outbox_stalled')",
    )
    op.create_table(
        "outbox_submissions",
        sa.Column("idempotency_key", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    """Drop outbox_submissions, and every outbox_stalled alert with the kind."""
    op.drop_table("outbox_submissions")
    op.execute("DELETE FROM alerts WHERE kind = 'outbox_stalled'")
    op.drop_constraint("ck_alerts_kind", "alerts", type_="check")
    op.create_check_constraint("ck_alerts_kind", "alerts", "kind IN ('mail', 'reminder')")
