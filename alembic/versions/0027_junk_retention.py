"""Generalise trash_entries into retention_entries, and add
account_prefs.junk_retention_days -- the same "time spent sitting in the
folder" retention Trash already has, now also available for Junk, with
its own independently configurable period rather than one setting
applied to two folders.

trash_entries becomes retention_entries with a role column ('trash' /
'junk'): one stamped-entry-time table shared by both sweeps rather than
a second, parallel copy of the same mechanism -- the two would agree the
day they were written and drift apart the first time only one of them
was fixed. Every existing row is a Trash entry by construction (nothing
else has ever written this table), so the backfill is unconditional.

Revision ID: 0027_junk_retention
Revises: 0026_trash_entries
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0027_junk_retention"
down_revision: str | None = "0026_trash_entries"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Rename trash_entries -> retention_entries, add its role column,
    and add account_prefs.junk_retention_days."""
    op.rename_table("trash_entries", "retention_entries")
    op.alter_column("retention_entries", "entered_trash_at", new_column_name="entered_at")
    op.add_column(
        "retention_entries",
        sa.Column("role", sa.Text, nullable=False, server_default="trash"),
    )
    op.alter_column("retention_entries", "role", server_default=None)
    op.execute(
        "ALTER INDEX idx_trash_entries_account_id "
        "RENAME TO idx_retention_entries_account_id"
    )

    op.add_column(
        "account_prefs", sa.Column("junk_retention_days", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Drop account_prefs.junk_retention_days, and reverse the rename."""
    op.drop_column("account_prefs", "junk_retention_days")
    op.execute(
        "ALTER INDEX idx_retention_entries_account_id "
        "RENAME TO idx_trash_entries_account_id"
    )
    op.drop_column("retention_entries", "role")
    op.alter_column("retention_entries", "entered_at", new_column_name="entered_trash_at")
    op.rename_table("retention_entries", "trash_entries")
