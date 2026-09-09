"""Reject a Trash or Junk retention period below one day at the database
itself, not only in the API schema.

Zero means every retention_entries row already stamped reads as overdue,
and a negative period puts the threshold in the future -- either one
clears the whole folder on the very next sweep tick, including an entry
stamped in that same transaction. The API's own Field(ge=1) was the only
thing standing between a typo'd setting and that, and it reaches neither
a raw SQL client nor -- had one existed -- an MCP tool bypassing the
Pydantic schema. The invariant now lives once, in the one place nothing
can write around it.

Revision ID: 0028_retention_min_days
Revises: 0027_junk_retention
"""

from __future__ import annotations

from alembic import op

revision: str = "0028_retention_min_days"
down_revision: str | None = "0027_junk_retention"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add the floor to both retention periods."""
    op.create_check_constraint(
        "ck_account_prefs_trash_retention_days_min",
        "account_prefs",
        "trash_retention_days IS NULL OR trash_retention_days >= 1",
    )
    op.create_check_constraint(
        "ck_account_prefs_junk_retention_days_min",
        "account_prefs",
        "junk_retention_days IS NULL OR junk_retention_days >= 1",
    )


def downgrade() -> None:
    """Drop the floor from both retention periods."""
    op.drop_constraint(
        "ck_account_prefs_junk_retention_days_min", "account_prefs", type_="check",
    )
    op.drop_constraint(
        "ck_account_prefs_trash_retention_days_min", "account_prefs", type_="check",
    )
