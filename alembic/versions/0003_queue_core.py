"""Queue core: queue_state and circuit_breakers.

Both are infrastructure for the generic work-queue engine (queue/), not for
any one queue -- queue_state tracks the operator-controlled lifecycle
(pause/resume, concurrency, batch size) of a named queue, and
circuit_breakers tracks the health of a named provider gate that any number
of queues can share. Neither table carries a foreign key onto a
PostIMAP-owned table, matching every other MailVerdict-owned table.

Revision ID: 0003_queue_core
Revises: 0002_verdict_msg_key
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_queue_core"
down_revision = "0002_verdict_msg_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create queue_state and circuit_breakers."""
    op.create_table(
        "queue_state",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("state", sa.Text, nullable=False, server_default="running"),
        sa.Column("concurrency", sa.Integer, nullable=False, server_default="1"),
        sa.Column("batch_size", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("state IN ('running', 'paused')", name="ck_queue_state_state"),
        sa.CheckConstraint("concurrency >= 0", name="ck_queue_state_concurrency"),
        sa.CheckConstraint("batch_size >= 1", name="ck_queue_state_batch_size"),
    )

    op.create_table(
        "circuit_breakers",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("state", sa.Text, nullable=False, server_default="closed"),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('closed', 'open', 'suspended')", name="ck_circuit_breakers_state",
        ),
    )


def downgrade() -> None:
    """Drop queue_state and circuit_breakers."""
    op.drop_table("circuit_breakers")
    op.drop_table("queue_state")
