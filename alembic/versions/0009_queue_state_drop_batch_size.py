"""Drop queue_state.batch_size -- nothing read it.

The claim batch size a worker asks for is a property of the worker's own
loop, not of a value an operator could tune per queue: `heartbeat_while`
(queue/worker_loop.py) only ever extends the lease of the row it currently
wraps, so a batch claimed under one shared lease leaves every other row in
it unprotected while it waits its turn -- a control surface for a batch
size greater than one would only ever be able to reintroduce that. Every
registered queue claims one row at a time.

Revision ID: 0009_queue_state_drop_batch_size
Revises: 0008_pipeline_run_from_addr
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0009_queue_state_drop_batch_size"
down_revision: str | None = "0008_pipeline_run_from_addr"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Drop the column and its check constraint."""
    op.drop_constraint("ck_queue_state_batch_size", "queue_state", type_="check")
    op.drop_column("queue_state", "batch_size")


def downgrade() -> None:
    """Restore the column, defaulting existing rows to 1."""
    op.add_column(
        "queue_state",
        sa.Column("batch_size", sa.Integer, nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_queue_state_batch_size", "queue_state", "batch_size >= 1",
    )
