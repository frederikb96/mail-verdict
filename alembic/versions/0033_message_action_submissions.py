"""Record keyed message actions, so a retried action is answered once.

See MessageActionSubmission in database/models.py.

Revision ID: 0033_message_action_submissions
Revises: 0032_native_push
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0033_message_action_submissions"
down_revision: str | None = "0032_native_push"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create message_action_submissions and the index its pruning scans."""
    op.create_table(
        "message_action_submissions",
        sa.Column("idempotency_key", sa.Uuid(), primary_key=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("response", JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_message_action_submissions_created_at",
        "message_action_submissions",
        ["created_at"],
    )


def downgrade() -> None:
    """Drop message_action_submissions."""
    op.drop_table("message_action_submissions")
