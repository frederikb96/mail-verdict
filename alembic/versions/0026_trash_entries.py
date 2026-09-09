"""Add trash_entries -- the retention sweep's own "first seen in Trash"
clock, since retention means time in Trash and nothing in the mirror
records when a message was moved there.

MailVerdict-owned, no foreign key onto messages, consistent with every
other table here -- message_id is not a durable identifier across a
UIDVALIDITY resync either, so the sweep is written to tolerate an
orphaned row rather than to prevent one; see the model's own docstring.

Revision ID: 0026_trash_entries
Revises: 0025_trash_retention
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0026_trash_entries"
down_revision: str | None = "0025_trash_retention"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create trash_entries."""
    op.create_table(
        "trash_entries",
        sa.Column("message_id", sa.Uuid, primary_key=True),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column(
            "entered_trash_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("idx_trash_entries_account_id", "trash_entries", ["account_id"])


def downgrade() -> None:
    """Drop trash_entries."""
    op.drop_table("trash_entries")
