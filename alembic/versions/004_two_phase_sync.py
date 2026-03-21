"""Add headers_synced + body_synced to mails, composite index for cursor pagination.

Revision ID: 004
Revises: 003
Create Date: 2026-03-21

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "004"
down_revision: Union[str, Sequence[str], None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add two-phase sync columns and cursor pagination index."""
    # Add headers_synced and body_synced columns with defaults
    op.add_column(
        "mails",
        sa.Column("headers_synced", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "mails",
        sa.Column("body_synced", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # Backfill: existing mails from v0.2.x have full content
    op.execute("UPDATE mails SET headers_synced = true, body_synced = true WHERE id IS NOT NULL")

    # Composite index for cursor-based pagination: WHERE folder_id = X ORDER BY received_at DESC
    op.create_index(
        "idx_mail_folder_received",
        "mails",
        ["folder_id", sa.text("received_at DESC")],
    )


def downgrade() -> None:
    """Remove two-phase sync columns and cursor pagination index."""
    op.drop_index("idx_mail_folder_received", table_name="mails")
    op.drop_column("mails", "body_synced")
    op.drop_column("mails", "headers_synced")
